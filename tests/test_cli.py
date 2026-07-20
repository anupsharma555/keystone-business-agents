from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import keystone_agents.cli as cli
from keystone_agents.calendar_actions import CalendarActionPlan
from keystone_agents.cli import main
from keystone_agents.instruction_following import InstructionFollowingRepairOutput
from keystone_agents.manual_request import (
    infer_manual_request_plan,
    merge_manual_request_plan,
)
from keystone_agents.orchestrator.preflight_context import (
    MANUAL_REQUEST_PLAN_ENV,
    ORCHESTRATOR_PREFLIGHT_ENV,
    ORCHESTRATOR_ROUTE_RESULT_ENV,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.operational_context import ZoteroContextResult
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import SQLiteStore
from promptfoo.eval_database import (
    eval_case_status,
    import_promptfoo_results,
    list_eval_trace_events,
    record_slack_eval_run,
)
from promptfoo.human_review import list_human_reviews


def test_verified_provider_links_excludes_unverified_or_non_https_receipts() -> None:
    links = cli._verified_provider_links(
        [
            {
                "status": "success",
                "provider_link": "https://airtable.com/app1/rec1",
                "verification": {"passed": True},
            },
            {
                "status": "verification_failed",
                "provider_link": "https://calendar.test/unverified",
                "verification": {"passed": False},
            },
            {
                "status": "success",
                "url": "local://not-clickable",
                "verification": {"passed": True},
            },
            {
                "status": "success",
                "html_link": "https://calendar.test/event",
                "verification": {"passed": True},
            },
        ]
    )

    assert links == [
        "https://airtable.com/app1/rec1",
        "https://calendar.test/event",
    ]


def _fake_orchestrator_preflight(
    request_text,
    *,
    requested_agent=None,
    live_manual_plan=False,
    **kwargs,
):
    del live_manual_plan, kwargs
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    result = cli.route_request(request_text, manual_plan=plan)
    return cli.OrchestratorPreflight(
        request_text=str(request_text or ""),
        requested_agent=plan.requested_agent,
        advisory_only=plan.requested_agent not in {None, "orchestrator"},
        selected_agent=str(plan.requested_agent or result.route),
        blocked_by_orchestrator=bool(result.refused),
        execution_allowed=not bool(result.refused),
        block_kind="send" if result.refused else "",
        block_reason=result.stop_reason or "",
        manual_request_plan=plan,
        route_result=result,
    )


@pytest.mark.parametrize(
    "route",
    [
        "business_research_analyst",
        "opportunity_scout",
        "gmail_triage",
        "outreach_composer",
        "chief_of_staff",
    ],
)
def test_direct_specialist_routes_share_one_llm_constraint_repair(
    route,
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent=route,
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                interpretation="exact five-word answer",
                scope="answer",
                word_count_mode="exact",
                word_count=5,
            )
        ),
    )

    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "TestOutput",
                    "send_enabled": False,
                    "human_summary": "This response is much too long for the request.",
                    "output": {"summary": "Bounded specialist evidence."},
                }
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=InstructionFollowingRepairOutput(
                response_text="Agents follow natural instructions accurately."
            ),
            usage={},
            cost={},
            request_cache={},
        ),
    )

    exit_code = cli._run_ask_script_live(
        route,
        "Answer this in exactly five words.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / f'{route}.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == "Agents follow natural instructions accurately."
    assert payload["instruction_following"]["repair_attempted"] is True
    assert payload["instruction_following"]["repair_succeeded"] is True


def test_direct_specialist_provider_blocker_skips_llm_constraint_repair(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                interpretation="one useful point",
                scope="entire_response",
                item_count_mode="exact",
                minimum_items=1,
                maximum_items=1,
            )
        ),
    )
    summary = (
        "I checked the connected Gmail mailbox using exact and relaxed subject "
        "searches, but found no matching message. No mailbox data was changed."
    )
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "blocked",
                    "block_kind": "gmail_target_not_found",
                    "send_enabled": False,
                    "human_summary": summary,
                    "output_type": "GmailClarificationResult",
                    "output": {"status": "blocked", "summary": summary},
                }
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cli,
        "resolve_instruction_following_response",
        lambda *_args, **_kwargs: pytest.fail(
            "provider blockers must not spend an instruction-repair request"
        ),
    )

    exit_code = cli._run_ask_script_live(
        "gmail_triage",
        "Find the selected email and make one useful point.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / 'gmail-blocked.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "gmail_target_not_found"
    assert payload["human_summary"] == summary
    assert "instruction_following" not in payload


def test_parent_renderer_prefers_verified_child_provider_summary(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    correct_summary = (
        'Yes - "UT Course Orientation Session" is on your Google Calendar '
        "on 2026-08-22."
    )
    stale_summary = "Aug 15, 2026."
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "done",
                    "send_enabled": False,
                    "completion_confirmed": True,
                    "user_facing_result_verified": True,
                    "human_summary": correct_summary,
                    "slack_display_text": correct_summary,
                    "public_result": {
                        "status": "completed",
                        "completion_confirmed": True,
                        "provider_write_attempted": False,
                        "provider_receipt_verified": None,
                    },
                    "tool_receipts": [
                        {
                            "status": "success",
                            "operation": "resolve_calendar_event",
                            "event_reference": "UT Course Orientation Session",
                            "title": "UT Course Orientation Session",
                            "start_date": "2026-08-22",
                        }
                    ],
                    "output_type": "ChiefOfStaffResult",
                    "output": {
                        "summary": stale_summary,
                        "sources": [
                            {
                                "title": "Stale prior Calendar result",
                                "url": "https://example.invalid/prior-event",
                            }
                        ],
                    },
                }
            ),
            stderr="",
        ),
    )

    exit_code = cli._run_ask_script_live(
        "chief_of_staff",
        "What date is the UT Course Orientation Session?",
        ["unused-child-command"],
        json_output=True,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            provider_system="google_calendar",
            primary_target="UT Course Orientation Session",
        ),
        database_url=f"sqlite:///{tmp_path / 'provider-render.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == correct_summary
    assert payload["slack_display_text"] == correct_summary
    assert stale_summary not in payload["human_summary"]


@pytest.mark.parametrize(
    ("route", "output_type"),
    [
        ("airtable_context_agent", cli.AirtableContextResult),
        ("google_workspace_context_agent", cli.GoogleWorkspaceContextResult),
        ("zotero_context_agent", cli.ZoteroContextResult),
        ("rss_context_agent", cli.RssContextResult),
        ("preprints_context_agent", cli.PreprintsContextResult),
    ],
)
def test_context_agent_routes_share_one_llm_constraint_repair(
    route,
    output_type,
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent=route,
        target_agent=route,
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                interpretation="exact five-word answer",
                scope="answer",
                word_count_mode="exact",
                word_count=5,
            )
        ),
    )
    prompts: list[str] = []

    def fake_run_typed_sdk_sync(_agent, prompt, _schema, **_kwargs):
        prompts.append(prompt)
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            output_type(
                mode="llm",
                summary="This context response is deliberately much too long.",
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=InstructionFollowingRepairOutput(
                response_text="Agents follow natural instructions accurately."
            ),
            usage={},
            cost={},
            request_cache={},
        ),
    )

    exit_code = cli._run_ask_context_agent_live(
        route,
        "Answer this in exactly five words.",
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / f'{route}.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "Interpreted response constraints from Orchestrator planning" in prompts[0]
    assert payload["human_summary"] == "Agents follow natural instructions accurately."
    assert payload["instruction_following"]["repair_attempted"] is True
    assert payload["instruction_following"]["repair_succeeded"] is True


def test_cli_health_smoke(capsys) -> None:
    exit_code = main(["health", "--database-url", ":memory:"])

    assert exit_code == 0
    assert "Overall status:" in capsys.readouterr().out


def test_manual_plan_advice_preserves_gmail_as_first_stage() -> None:
    plan = infer_manual_request_plan(
        "Review Gmail today, select the strongest opportunity, then research the company.",
        requested_agent="gmail_triage",
    ).model_copy(
        update={
            "target_agent": "business_research_analyst",
            "intent": "gmail_triage",
        }
    )

    assert cli._route_with_manual_plan_advice("orchestrator", plan) == "gmail_triage"


def test_manual_plan_advice_hands_incompatible_scout_ask_to_research_owner() -> None:
    plan = infer_manual_request_plan(
        "who is Abridge and summarize the company in 20 words.",
        requested_agent="opportunity_scout",
    )

    assert cli._route_with_manual_plan_advice("opportunity_scout", plan) == (
        "business_research_analyst"
    )


def test_manual_plan_advice_preserves_chief_advisory_coordination() -> None:
    prompt = (
        "Use Airtable Context and Google Workspace Context as read-only advisors to "
        "design a handoff table. Do not write records or create files."
    )
    plan = infer_manual_request_plan(prompt, requested_agent="chief_of_staff").model_copy(
        update={"target_agent": "airtable_context_agent"}
    )

    assert cli._route_with_manual_plan_advice("chief_of_staff", plan) == "chief_of_staff"


@pytest.mark.parametrize(
    ("requested_agent", "prompt", "expected_owner"),
    [
        (
            "business_research_analyst",
            "Find current remote psychiatry grant opportunities.",
            "opportunity_scout",
        ),
        (
            "opportunity_scout",
            "Triage my unread Gmail messages from today.",
            "gmail_triage",
        ),
        (
            "gmail_triage",
            "Draft a LinkedIn outreach note using approved facts.",
            "outreach_composer",
        ),
        (
            "outreach_composer",
            "Review our Slack workflow status and recommend next actions.",
            "chief_of_staff",
        ),
    ],
)
def test_manual_plan_advice_executes_clear_owner_not_wrong_named_specialist(
    requested_agent: str,
    prompt: str,
    expected_owner: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=requested_agent)

    assert cli._route_with_manual_plan_advice(requested_agent, plan) == expected_owner


def test_cli_promptfoo_agent_eval_mode_disables_eval_helpers(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_PROMPTFOO_EVAL", raising=False)
    assert cli._promptfoo_agent_eval_mode() is False

    monkeypatch.setenv("KEYSTONE_PROMPTFOO_EVAL", "true")
    assert cli._promptfoo_agent_eval_mode() is True


def test_cli_langgraph_helper_preserves_manager_step_limit(monkeypatch) -> None:
    import keystone_agents.langgraph_workflow as langgraph_workflow

    captured: dict[str, object] = {}
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Graph result"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Graph path used.",
    )

    class FakeOutcome:
        result = graph_result

    def fake_run_work_item_langgraph(request, **kwargs):  # type: ignore[no-untyped-def]
        captured["request"] = request
        captured.update(kwargs)
        return FakeOutcome()

    monkeypatch.setattr(
        langgraph_workflow,
        "run_work_item_langgraph",
        fake_run_work_item_langgraph,
    )
    request = WorkflowRunRequest(request_text="@KNI chief of staff summarize eval gaps")

    outcome = cli._run_work_item_langgraph_for_request(request, max_manager_steps=1)

    assert outcome.result is graph_result
    assert captured["request"] is request
    assert captured["manager_loop"] is True
    assert captured["max_manager_steps"] == 1


def test_live_cos_multi_owner_plan_uses_work_item_without_named_specialists(
    monkeypatch,
) -> None:
    request = (
        "CoS, using these approved facts: direct tasks have one owner and tracked "
        "multi-step work preserves state. Review the architecture tradeoff, prioritize "
        "the largest validation gap, and draft an internal Slack update for my review. "
        "Do not search, send email, or post."
    )
    captured: dict[str, object] = {}

    def fake_preflight(request_text, *, requested_agent=None, **_kwargs):
        plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
        routed = cli.route_request(request_text, manual_plan=plan)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent=str(routed.route),
            execution_allowed=True,
            manual_request_plan=plan,
            route_result=routed,
        )

    def fake_work_item(input_text: str, **kwargs: object) -> int:
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)
    monkeypatch.setattr(
        cli,
        "_run_ask_specialist_live",
        lambda *_args, **_kwargs: pytest.fail(
            "multi-owner Chief ask must not run as one direct specialist"
        ),
    )

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "20",
            "--json",
            f"@KNI {request}",
        ]
    )

    assert exit_code == 0
    assert captured["input_text"] == request.removeprefix("CoS, ")
    assert captured["requested_route"] == "business_research_analyst"
    assert captured["manual_plan"].workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]


def test_metadata_only_work_item_summary_is_not_reported_as_completion(
    capsys,
) -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Internal architecture review",
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="WorkItem command completed.",
    )

    exit_code = cli._print_work_item_result(result, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["user_facing_result_verified"] is False
    assert payload["completion_confirmed"] is False
    assert payload["slack_display_title"] == "Business Agents Completion Not Confirmed"
    assert "Completion is not confirmed" in payload["slack_display_text"]
    assert "WorkItem command completed" not in payload["slack_display_text"]


def test_substantive_done_work_item_summary_is_verified_for_slack(
    capsys,
) -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Internal architecture review",
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary=(
            "The architecture keeps simple tasks direct and uses tracked state only "
            "when several owners must contribute."
        ),
    )

    exit_code = cli._print_work_item_result(result, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["user_facing_result_verified"] is True
    assert payload["completion_confirmed"] is True
    assert payload["slack_display_title"] == "Business Agents Result Ready"
    assert payload["slack_display_text"] == payload["human_summary"]


def test_cli_init_db_uses_explicit_database_url(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "keystone.db"

    exit_code = main(["init-db", "--database-url", f"sqlite:///{database_path}"])

    assert exit_code == 0
    assert database_path.exists()
    assert "Initialized SQLite database:" in capsys.readouterr().out


def test_cli_automations_list_and_audit(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'automations.db'}"

    list_exit = main(["automations", "list", "--database-url", database_url])
    audit_exit = main(["automations", "audit", "--database-url", database_url, "--json"])

    assert list_exit == 0
    assert audit_exit == 0
    output = capsys.readouterr().out
    assert "Weekly Opportunity Scan" in output
    assert "automation_specs" in output


def test_cli_route_stays_dry_run(capsys) -> None:
    exit_code = main(["route", "--input", "research Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: orchestrator" in output
    assert "Send enabled: False" in output


def test_cli_ask_routes_unmentioned_input_through_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask.db'}"
    exit_code = main(["ask", "--database-url", database_url, "research", "Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Manual plan: business_research_analyst / company_research" in output
    assert "Artifacts: company_profile:" in output


def test_cli_genuine_clarification_returns_direct_public_result_without_workitem(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'clarification.db'}"

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--no-live-manual-plan",
            "--json",
            "--database-url",
            database_url,
            "do",
            "that",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_input"
    assert payload["route"] == "clarification"
    assert payload["public_result"]["status"] == "needs_input"
    assert "work_item" not in payload
    assert "route_not_supported_in_workitem_phase" not in json.dumps(payload)


def test_cli_preflight_receives_selected_slack_thread_before_work_item_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context_file = tmp_path / "slack-zotero-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "source": "test",
                "channel_id": "C123",
                "channel_name": "ai-agents-workflow",
                "selected_message_ts": "1715366400.000100",
                "thread_ts": "1715366400.000100",
                "selected_message": {
                    "ts": "1715366400.000100",
                    "user_id": "U123",
                    "text": "One exact Zotero article, KBA_TEST_ARTICLE, was resolved.",
                },
                "thread_messages": [
                    {
                        "ts": "1715366400.000100",
                        "user_id": "U123",
                        "text": "One exact Zotero article, KBA_TEST_ARTICLE, was resolved.",
                    }
                ],
                "thread_fetch_status": "ok",
            }
        ),
        encoding="utf-8",
    )
    actual_preflight = cli.run_orchestrator_preflight
    captured: dict[str, object] = {}

    def capture_preflight(request_text, **kwargs):
        captured["workflow_state"] = kwargs.get("workflow_state")
        result = actual_preflight(request_text, **kwargs)
        captured["manual_plan"] = result.manual_request_plan
        return result

    monkeypatch.setattr(cli, "run_orchestrator_preflight", capture_preflight)

    exit_code = main(
        [
            "ask",
            "--context-file",
            str(context_file),
            "@KNI CoS Attach this PDF to the article.",
        ]
    )

    assert exit_code == 0
    workflow_state = captured["workflow_state"]
    assert isinstance(workflow_state, dict)
    assert "KBA_TEST_ARTICLE" in str(workflow_state["slack_thread_transcript"])
    plan = captured["manual_plan"]
    assert isinstance(plan, ManualRequestPlan)
    assert plan.target_agent == "zotero_context_agent"
    assert plan.intent == "business_system_write"


def test_cli_live_manual_plan_only_honors_openai_request_ceiling(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "run_orchestrator_preflight",
        lambda *_args, **_kwargs: pytest.fail("preflight should be blocked before a model call"),
    )

    exit_code = main(
        [
            "ask",
            "--live-manual-plan",
            "--max-openai-requests",
            "0",
            "--json",
            "@KNI CoS Add this link to the notes.",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["estimated_requests"] == {
        "max": 1,
        "min": 1,
        "stages": ["manual_request_planner"],
    }


def test_live_manual_plan_gets_one_request_before_resolved_route_budget(
    monkeypatch,
) -> None:
    request = (
        "Without searching or using provider tools, use only these two facts: "
        "negative constraints narrow execution, and the offline suite passes. "
        "Give me exactly two short bullets. Do not draft outreach or modify records."
    )
    fallback = infer_manual_request_plan(request)
    bad_live_candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "clarification",
            "intent": "clarification",
            "task_objective": "clarification",
        }
    )
    calls: list[str] = []
    specialist_calls: list[str] = []

    def fake_preflight(request_text, **_kwargs):
        calls.append(request_text)
        merged = merge_manual_request_plan(fallback, bad_live_candidate)
        route_result = cli.route_request(request_text, manual_plan=merged)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=merged.requested_agent,
            selected_agent=merged.target_agent,
            manual_request_plan=merged,
            route_result=route_result,
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_specialist(route, *_args, **_kwargs):
        specialist_calls.append(route)
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_specialist)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--live-manual-plan",
            "--max-openai-requests",
            "5",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert calls == [request]
    assert specialist_calls == ["chief_of_staff"]


def test_resolved_live_workflow_is_authoritative_for_post_planner_budget(
    monkeypatch,
) -> None:
    request = (
        "CoS, use the supplied company note to prepare the coordinated decision brief "
        "and a copyable internal note. Do not search or use provider tools."
    )
    resolved_plan = infer_manual_request_plan(
        (
            "CoS, use only this note. Give me what is supported, the strongest "
            "potential KNI advisory or research fit, the single validation question "
            "that should come first, and a short internal Slack note I can paste. "
            "Do not search or post."
        ),
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "source": "llm",
            "objective": request,
        }
    )
    assert resolved_plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    captured: dict[str, object] = {}

    def fake_preflight(request_text, **_kwargs):
        route_result = cli.route_request(request_text, manual_plan=resolved_plan)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent="chief_of_staff",
            selected_agent="chief_of_staff",
            manual_request_plan=resolved_plan,
            route_result=route_result,
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_work_item(input_text, **kwargs):
        captured["input_text"] = input_text
        captured["manual_plan"] = kwargs["manual_plan"]
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--live-manual-plan",
            "--live-search",
            "--max-openai-requests",
            "5",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert captured["manual_plan"] is resolved_plan


def test_budget_message_reports_planner_request_before_resolved_route_block(
    capsys,
) -> None:
    exit_code = cli._print_ask_request_budget_blocked(
        json_output=True,
        requested_limit=5,
        estimate={
            "min": 2,
            "max": 7,
            "stages": ["manual_request_planner", "business_research_analyst_sdk"],
        },
        openai_requests_made=1,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["openai_requests_made"] == 1
    assert "used 1 bounded planning request" in payload["message"]
    assert "before any model call" not in payload["message"]
    assert "No specialist or provider action ran" in payload["message"]


def test_live_semantic_preflight_can_narrow_unnamed_ask_to_direct_specialist(
    monkeypatch,
    capsys,
) -> None:
    request = "Look into NeuroFlow and tell me whether it is relevant to KNI."
    calls: dict[str, object] = {}
    plan = ManualRequestPlan(
        source="llm",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target="NeuroFlow",
        target_type="company",
        objective="Research NeuroFlow and assess its relevance to KNI.",
        task_objective="entity_research",
        expected_artifact_type="research_brief",
    )
    route_result = cli.route_request(request, manual_plan=plan)

    def fake_preflight(request_text, **kwargs):
        calls["preflight"] = {"request_text": request_text, **kwargs}
        return cli.OrchestratorPreflight(
            request_text=request_text,
            selected_agent="business_research_analyst",
            manual_request_plan=plan,
            route_result=route_result,
        )

    def fake_direct(route, input_text, **kwargs):
        calls["direct"] = {"route": route, "input_text": input_text, **kwargs}
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_direct)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "8",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert calls["preflight"]["live_manual_plan"] is True
    assert calls["direct"]["route"] == "business_research_analyst"
    assert calls["direct"]["manual_plan"] is plan


def test_cli_ask_json_reports_actual_execution_and_graph_metadata(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-execution.db'}"
    monkeypatch.setenv("KEYSTONE_WORKITEM_LANGGRAPH", "true")

    exit_code = main(
        [
            "ask",
            "--json",
            "--live-sdk",
            "--max-openai-requests",
            "0",
            "--database-url",
            database_url,
            (
                "Create a temporary Sheet named KBA_TEST_SHEET execution-metadata "
                "in KNIOps and prepare its cleanup plan. Run deterministically with "
                "no live SDK or model calls."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_execution"] == {
        "langgraph": True,
        "live_sdk": False,
        "live_search": False,
        "openai_requests": 0,
    }
    assert payload["_langgraph"]["runtime"] in {
        "langgraph",
        "dependency_free_fallback",
    }
    assert "run_chief_of_staff" in payload["_langgraph"]["node_path"]


def test_cli_ask_eval_score_template_returns_prompted_rubric_json(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "score",
            "template",
            "case",
            "slack_behavioral_health_rfp_001",
            "run",
            "sbar_example",
            "agent",
            "business_research_analyst",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["status"] == "done"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_example"
    assert payload["agent"] == "business_research_analyst"
    assert payload["slack_channel_id"] == "C0BA17Y9C01"
    assert "Score each dimension 0-5" in payload["human_summary"]
    assert "readability:" in payload["human_summary"]
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["dashboard_url"] == "http://127.0.0.1:8769/dashboard"
    assert payload["eval_thread_reply"]["scorecard_request"] == (
        "@KNI can you give me a scorecard for this eval?"
    )
    assert payload["send_enabled"] is False


def test_cli_ask_eval_score_template_plain_text(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "@KNI",
            "eval",
            "scoring",
            "rubric",
            "case:slack_case_1",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.startswith("eval score")
    assert "case: slack_case_1" in output
    assert "metadata-heavy" in output


def test_cli_ask_eval_score_template_without_case_blocks_with_thread_guidance(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "score",
            "template",
            "case",
            "<case_id>",
            "run",
            "<run_id>",
            "agent",
            "<agent_name>",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["status"] == "blocked"
    assert "could not resolve the eval case" in payload["human_summary"]
    assert "@KNI can you give me a scorecard for this eval?" in payload["human_summary"]


def test_cli_ask_eval_score_template_handles_wrapped_slack_followup(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "workitem",
            "opportunity_scout",
            "continue",
            "this",
            "prior",
            "Slack",
            "thread.",
            "Linked",
            "WorkItem:",
            "wi_example",
            "Follow-up:",
            "eval",
            "score",
            "template",
            "case",
            "slack_behavioral_health_rfp_001",
            "run",
            "sbar_wrapped",
            "agent",
            "business_research_analyst",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_wrapped"
    assert "accuracy:" in payload["human_summary"]


def test_cli_ask_eval_scorecard_infers_case_from_slack_thread_context(
    tmp_path: Path,
    capsys,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": (
                    "Slack thread history digest:\n"
                    "Thread messages, oldest first:\n"
                    "- ts=1781202023.470699 author=UUSER title=Eval request: "
                    "@KNI business research analyst: eval case "
                    "slack_behavioral_health_rfp_001 Find 3 current grants/RFPs.\n"
                    "- ts=1781202025.859599 author=BKNI title=Business Agents Run Completed: "
                    "Run: sbar_bd1fe1d83aca4f93864a03c0618c5dfc "
                    "WorkItem: wi_e59cb39aeac24ec49d012b2f27cb244b "
                    "Route: business_research_analyst\n"
                ),
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            "can",
            "you",
            "give",
            "me",
            "a",
            "scorecard",
            "for",
            "this",
            "eval?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"
    assert payload["agent"] == "business_research_analyst"
    assert "case: slack_behavioral_health_rfp_001" in payload["human_summary"]


def test_cli_ask_eval_score_reply_saves_human_review(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781201599.554659",
            }
        ),
        encoding="utf-8",
    )
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="sbar_example",
        agent="business_research_analyst",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781201599.554659",
        request_text="business research analyst eval case slack_behavioral_health_rfp_001",
        result_summary="Business Agents Run Completed",
        database_path=review_db,
    )
    score_block = """eval score
case: slack_behavioral_health_rfp_001
run: sbar_example
agent: business_research_analyst
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass
notes: Useful and source-backed enough for a first pass."""

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_block,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["status"] == "done"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_example"
    assert payload["average_score"] == 4.455
    assert payload["slack_channel_name"] == "evals"
    assert payload["slack_thread_ts"] == "1781201599.554659"
    assert payload["database_path"] == str(review_db)
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["refresh_targets"] == ["overview", "database", "runs_scoring", "analysis"]
    assert payload["trace_event_type"] == "human_review_saved"
    assert payload["eval_thread_reply"]["submit_evaluation_action"] == "Submit Evaluation"
    assert [
        action["action_id"] for action in payload["eval_thread_reply"]["slack_actions"]
    ] == ["kba_eval_review", "kba_eval_orchestrator_judge"]
    assert "human notes" in payload["eval_thread_reply"]["submit_evaluation_effect"]
    assert payload["eval_thread_reply"]["refresh_targets"] == [
        "overview",
        "database",
        "runs_scoring",
        "analysis",
    ]
    assert (tmp_path / "dashboard.html").exists()

    stored = list_human_reviews(database_path=review_db)
    assert len(stored) == 1
    assert stored[0]["scores"]["accuracy"] == 4
    assert stored[0]["scores"]["readability"] == 5


def test_cli_ask_natural_eval_score_reply_uses_slack_thread_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": (
                    "Original request: eval case slack_behavioral_health_rfp_001\n"
                    "Business Agents Run Completed Run: sbar_example "
                    "WorkItem: wi_e59cb39aeac24ec49d012b2f27cb244b "
                    "Route: business_research_analyst"
                ),
            }
        ),
        encoding="utf-8",
    )
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="wi_e59cb39aeac24ec49d012b2f27cb244b",
        agent="business_research_analyst",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781202023.470699",
        request_text="business research analyst eval case slack_behavioral_health_rfp_001",
        result_summary="Business Agents Run Completed",
        database_path=review_db,
    )

    score_reply = """Here are my scores:
accuracy 4
relevance 5
explainability 4
readability 5
source_quality 4
search_quality 4
synthesis 4
output 4
format 5
instruction_following 5
usefulness 5
safety pass
notes: Good enough for a first pass."""
    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"
    assert payload["agent"] == "business_research_analyst"
    assert payload["average_score"] == 4.455
    assert payload["notes"] == "Good enough for a first pass."
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    events = list_eval_trace_events(database_path=review_db, limit=5)
    review_event = next(event for event in events if event["event_type"] == "human_review_saved")
    assert review_event["group_id"] == "slack_behavioral_health_rfp_001"
    assert review_event["metadata"]["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"


def test_cli_ask_eval_score_reply_requires_recorded_response(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    score_reply = """eval score
case: slack_behavioral_health_rfp_001
run: wi_missing_response
agent: business_research_analyst
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass
notes: This should not save without a recorded response."""

    exit_code = main(["ask", "--json", "@KNI", score_reply])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["status"] == "blocked"
    assert "recorded Promptfoo or Slack response" in payload["error"]


def test_cli_ask_eval_status_reports_promptfoo_and_human_scores(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-example",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_behavioral_health_rfp_001",
                                "agent_under_test": "opportunity_scout",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=review_db)
    score_block = """eval score
case: slack_behavioral_health_rfp_001
run: eval-example
agent: opportunity_scout
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass"""
    main(["ask", "@KNI", score_block])
    capsys.readouterr()

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "status",
            "case",
            "slack_behavioral_health_rfp_001",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_status"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert "Promptfoo pass" in payload["human_summary"]
    assert "average 4.455/5" in payload["human_summary"]
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert "<http://127.0.0.1:8769/dashboard?case=slack_behavioral_health_rfp_001|case dashboard>" in payload["human_summary"]
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["scorecard_request"] == "@KNI can you give me a scorecard for this eval?"
    assert payload["eval_thread_reply"]["submit_evaluation_action"] == "Submit Evaluation"


def test_cli_ask_natural_eval_status_infers_case_from_slack_thread_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-example",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_behavioral_health_rfp_001",
                                "agent_under_test": "opportunity_scout",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=review_db)
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": "Prior ask: eval case slack_behavioral_health_rfp_001",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            "how",
            "is",
            "this",
            "eval",
            "doing?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_status"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert "Promptfoo pass" in payload["human_summary"]
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["scorecard_request"] == "@KNI can you give me a scorecard for this eval?"


def test_hidden_eval_case_does_not_hijack_business_cleanup_status_request(
    tmp_path: Path,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1783811494.407029",
                "read_context": "Hidden eval case slack_create_one_temporary_001",
            }
        ),
        encoding="utf-8",
    )

    payload = cli._eval_status_payload(
        (
            "Create one temporary Sheet, verify provider operations, and return "
            "the cleanup status and graph evidence."
        ),
        context_file_path=str(context_file),
    )

    assert payload is None


def test_cli_ask_agent_override_ignores_eval_context_without_eval_request(
    tmp_path: Path,
    capsys,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0ASJ6QU1FX",
                "channel_name": "ai-agents-workflow",
                "thread_ts": "1781818754.293029",
                "read_context": "Prior eval case slack_behavioral_health_rfp_001",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "opportunity_scout",
            "--json",
            "--context-file",
            str(context_file),
            "flexible",
            "case",
            "flex_opp_20260618_001_natural_opportunity_directions",
            "Can",
            "you",
            "look",
            "for",
            "2",
            "realistic",
            "Keystone",
            "opportunity",
            "directions",
            "around",
            "clinical",
            "AI",
            "evaluation?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("mode") != "eval_status"
    assert payload["selected_agent"] == "opportunity_scout"
    assert payload["route"] == "opportunity_scout"


def test_cli_ask_eval_case_with_slack_context_records_slack_eval_run(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "selected_message_ts": "1781202023.470699",
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "business",
            "research",
            "analyst:",
            "eval",
            "case",
            "slack_behavioral_health_rfp_001",
            "research",
            "Curebase",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_eval_record"]["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["_eval_record"]["slack_thread_ts"] == "1781202023.470699"
    assert payload["_eval_record"]["run_id"].startswith("wi_")
    assert payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert payload["_eval_record"]["scorecard_request"] == (
        "@KNI can you give me a scorecard for this eval?"
    )
    assert payload["_eval_record"]["dashboard_visibility"]["case_visible"] is True
    assert payload["_eval_record"]["dashboard_visibility"]["latest_run_visible"] is True
    assert payload["_eval_record"]["dashboard_visibility"]["trace_event_visible"] is True
    assert payload["_eval_record"]["post_save_state"]["merged_status"]["slack_run_count"] == 1
    assert payload["_eval_record"]["post_save_state"]["trace"]["manual_run_summary_present"] is True
    assert "/api/status?refresh=1" in payload["_eval_record"]["refresh_endpoints"]
    assert "/api/trace-diagnostics" in payload["_eval_record"]["refresh_endpoints"]
    assert payload["_eval_record"]["eval_thread_reply"]["dashboard_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert [
        action["action_id"]
        for action in payload["_eval_record"]["eval_thread_reply"]["slack_actions"]
    ] == ["kba_eval_review", "kba_eval_orchestrator_judge"]
    assert payload["_eval_record"]["review_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert "Eval: case `slack_behavioral_health_rfp_001`" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/dashboard?case=slack_behavioral_health_rfp_001|case dashboard>" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/review?case=slack_behavioral_health_rfp_001|score this case>" in payload["human_summary"]
    assert "press `Submit Evaluation` in Slack" in payload["human_summary"]

    status = eval_case_status("slack_behavioral_health_rfp_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["run_id"] == payload["_eval_record"]["run_id"]
    assert status["slack_runs"][0]["agent"] == "business_research_analyst"
    assert status["slack_runs"][0]["work_item_id"] == payload["_eval_record"]["run_id"]
    assert status["slack_runs"][0]["thread_fetch_status"] in {"ok", "not_requested"}
    assert status["slack_runs"][0]["thread_message_count"] >= 0
    assert status["slack_runs"][0]["response_hash"]
    assert status["slack_runs"][0]["evidence"]["schema"] == "keystone.slack.eval_evidence.v1"


def test_cli_ask_records_hidden_eval_case_from_slack_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "request_ts": "1781206953.875749",
                "thread_fetch_status": "ok",
                "thread_messages": [
                    {
                        "ts": "1781206953.875749",
                        "user": "U123",
                        "text": "opportunity scout eval case",
                    }
                ],
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_eval_record"]["case_id"] == "slack_agents_sdk_course_001"
    assert payload["_eval_record"]["slack_thread_ts"] == "1781206953.875749"
    assert payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_agents_sdk_course_001"
    )
    assert "Eval: case `slack_agents_sdk_course_001`" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/dashboard?case=slack_agents_sdk_course_001|case dashboard>" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/review?case=slack_agents_sdk_course_001|score this case>" in payload["human_summary"]

    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["request_text"].startswith("find three Agents SDK courses")
    assert "eval case" not in status["slack_runs"][0]["request_text"].lower()
    assert status["slack_runs"][0]["thread_fetch_status"] == "ok"
    assert status["slack_runs"][0]["thread_message_count"] == 1
    assert status["slack_runs"][0]["response_hash"]
    assert status["slack_runs"][0]["evidence"]["source"] == "cli_slack_context"


def test_cli_hidden_eval_case_records_run_and_natural_human_review(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "request_ts": "1781206953.875749",
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    run_exit = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost",
        ]
    )

    assert run_exit == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["_eval_record"]["run_id"]
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "read_context": (
                    "Slack thread history digest\n"
                    f"Business Agents WorkItem Advanced WorkItem: {run_id} "
                    "Route: opportunity_scout\n"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    score_reply = """Here are my scores:
accuracy 4
relevance 4
explainability 5
readability 5
source_quality 4
search_quality 4
synthesis 4
output 5
format 5
instruction_following 5
usefulness 5
safety pass
notes: Strong answer, but course price recency should be rechecked."""

    score_exit = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert score_exit == 0
    score_payload = json.loads(capsys.readouterr().out)
    assert score_payload["mode"] == "eval_score_saved"
    assert score_payload["case_id"] == "slack_agents_sdk_course_001"
    assert score_payload["run_id"] == run_id
    assert score_payload["agent"] == "opportunity_scout"
    assert score_payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert score_payload["dashboard_case_url"].endswith("?case=slack_agents_sdk_course_001")

    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["latest_human_review"]["case_id"] == "slack_agents_sdk_course_001"
    assert status["latest_human_review"]["run_id"] == run_id
    assert status["latest_human_review"]["safety"] == "pass"


def test_cli_eval_channel_natural_ask_reuses_promptfoo_case_and_scores_by_thread(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-course",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK "
                                    "courses that are reasonably cost and good for "
                                    "someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=review_db)
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781207777.000100",
                "request_ts": "1781207777.000100",
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    run_exit = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost "
            "and good for someone with some experience",
        ]
    )

    assert run_exit == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["_eval_record"]["run_id"]
    assert run_payload["_eval_record"]["case_id"] == "slack_agents_sdk_course_001"
    assert run_payload["_eval_record"]["dashboard_path"] == str(
        (tmp_path / "dashboard.html").resolve()
    )
    assert run_payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_agents_sdk_course_001"
    )
    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert "eval case" not in status["slack_runs"][0]["request_text"].lower()

    score_reply = """Looks good:
accuracy 4
relevance 5
explainability 5
readability 5
source_quality 4
search_quality 4
synthesis 4
output 5
format 5
instruction_following 5
usefulness 5
safety pass
notes: This is readable and useful."""
    score_exit = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert score_exit == 0
    score_payload = json.loads(capsys.readouterr().out)
    assert score_payload["mode"] == "eval_score_saved"
    assert score_payload["case_id"] == "slack_agents_sdk_course_001"
    assert score_payload["run_id"] == run_id
    assert score_payload["agent"] == "opportunity_scout"
    assert score_payload["notes"] == "This is readable and useful."


def test_cli_work_items_advance_zotero_collection_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    collection_key = "LTA3U8I8"
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS & Lindus Trial Context": collection_key}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "DOI": "10.1000/example",
                            "abstractNote": "A randomized trial tested home-based tDCS for MDD.",
                            "itemType": "journalArticle",
                            "collections": [collection_key],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analyst to summarize the Zotero collection "
                "'LH 01 - REACH-tDCS & Lindus Trial Context' with one paragraph per source"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "Remote tDCS randomized trial" in output


def test_cli_work_items_advance_uses_orchestrator_preflight_and_manager_loop(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--json",
            "--input",
            "research Curebase",
            "--database-url",
            database_url,
            "--max-manager-steps",
            "1",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "business_research_analyst"
    assert payload["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["preflight_memo"]["raw_request"] == (
        "research Curebase"
    )

    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    assert review_events
    assert review_events[0].metadata["route"] == "business_research_analyst"
    assert review_events[0].metadata["review_decision"] in {"pass", "warn", "block"}


def test_cli_ask_colon_named_business_research_does_not_auto_handoff_to_scout(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "research",
            "analyst:",
            "diagnostic",
            "case",
            "diag_business_research_no_live_route_001",
            "Research",
            "Abridge",
            "as",
            "a",
            "clinical",
            "documentation",
            "AI",
            "company.",
            "Return",
            "a",
            "concise",
            "Answer,",
            "Detailed",
            "Summary,",
            "and",
            "Useful",
            "references.",
            "Focus",
            "on",
            "product/workflow,",
            "healthcare",
            "buyer",
            "fit,",
            "evidence",
            "or",
            "deployment",
            "signals,",
            "and",
            "what",
            "remains",
            "unverified.",
            "Do",
            "not",
            "draft",
            "outreach,",
            "send,",
            "schedule,",
            "write",
            "files,",
            "create",
            "CRM",
            "records,",
            "publish,",
            "or",
            "post",
            "elsewhere.",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Manual plan: business_research_analyst / company_research" in output
    assert "Route: business_research_analyst" in output
    assert "Research: Abridge" in output
    assert "*Answer:*" in output
    assert "Abridge has source-backed company context" in output
    assert "Route: opportunity_scout" not in output
    assert "step 2 opportunity_scout" not in output
    stored_items = SQLiteStore(database_url).list_work_items(limit=1)
    assert stored_items
    assert stored_items[0].title == "Research: Abridge"


def test_cli_work_items_advance_preflight_uses_existing_specialist_route(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"
    store = SQLiteStore(database_url)
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.BLOCKED,
        title="Research: OpenEvidence",
        request_text="business research analyst research OpenEvidence",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        last_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    )
    store.save_work_item(work_item)
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["request_text"] = request_text
        captured["requested_agent"] = requested_agent
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_advance_work_item_manager_loop(request, **_kwargs):
        captured["manual_request_plan"] = request.manual_request_plan
        return WorkflowRunResult(
            work_item=work_item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.BLOCKED,
            advanced=True,
            human_summary="Specialist continuation accepted.",
            manual_request_plan=request.manual_request_plan,
            orchestrator_preflight=request.orchestrator_preflight,
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "false")
    monkeypatch.setattr(
        cli,
        "advance_work_item_manager_loop",
        fake_advance_work_item_manager_loop,
    )

    exit_code = main(
        [
            "work-items",
            "advance",
            work_item.id,
            "--json",
            "--live-sdk",
            "--input",
            "business research analyst research OpenEvidence\nFollow-up: look at partnerships",
            "--database-url",
            database_url,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["requested_agent"] == "business_research_analyst"
    assert captured["live_manual_plan"] is True
    assert captured["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"


def test_cli_work_items_advance_zotero_article_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "AP9SKRPZ",
                            "title": (
                                "Study Details | NCT06976697 | Home-Based tDCS "
                                "Treatment Of Major Depressive Disorder"
                            ),
                            "url": "https://clinicaltrials.gov/study/NCT06976697",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analysit to find and summarize the Zotero "
                "article on the Lindus SOOMA trial. Include one paragraph summary, "
                "methods/design, inclusion/exclusion, and other relevant trial info"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "NCT06976697" in output


def test_cli_ask_kni_mention_uses_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-kni.db'}"
    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "agent",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Advanced: True" in output


def test_cli_ask_generic_kni_opportunity_to_outreach_runs_workflow(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-loop.db'}"
    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "@KNI",
            "run",
            "one",
            "opportunity-to-outreach",
            "loop",
            "for",
            "behavioral",
            "health",
            "AI.",
            "Top",
            "1",
            "only.",
            "Post",
            "approval",
            "to",
            "this",
            "channel.",
            "Draft",
            "only,",
            "do",
            "not",
            "send.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"] == "opportunity_to_outreach_loop"
    assert payload["selected_agent"] == "orchestrator"
    assert payload["topic"] == "behavioral health AI"
    assert payload["top_n"] == 1
    assert payload["send_enabled"] is False
    assert len(payload["output"]["items"]) == 1
    posts = payload["output"]["storage"]["slack_approval_posts"]
    assert len(posts) == 1
    assert posts[0]["status"] == "dry-run"
    assert posts[0]["send_enabled"] is False


def test_cli_ask_agent_override_keeps_direct_dry_run(capsys) -> None:
    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Mode: dry_run" in output


def test_cli_ask_agent_override_selects_specialist_json(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "opportunity_scout",
            "--json",
            "find psychiatry AI opportunities",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"selected_agent": "opportunity_scout"' in output
    assert '"send_enabled": false' in output


def test_cli_ask_context_agent_mention_uses_first_class_dry_run(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "airtable",
            "context",
            "agent",
            "identify",
            "schema",
            "mapping",
            "and",
            "record",
            "identity",
            "questions",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "airtable_context_agent"
    assert payload["route"] == "airtable_context_agent"
    assert payload["status"] == "done"
    assert payload["output"]["agent_name"] == "airtable_context_agent"
    assert payload["output"]["mode"] == "deterministic"
    assert "schema mapping" in payload["human_summary"]
    assert "record identity" in payload["human_summary"]
    assert payload["send_enabled"] is False
    assert payload["side_effects"]["external_write_performed"] is False


def test_cli_ask_kni_multistep_uses_backend_selected_langgraph(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'kni_multistep_graph.db'}"
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "@KNI",
            "research",
            "NeuroFlow",
            "and",
            "find",
            "matching",
            "opportunities",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")

    assert exit_code == 0
    assert payload["route"] in {
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    }
    assert any(event.event_type == "langgraph_orchestration" for event in events)
    assert any(event.event_type == "langgraph_manager_loop_completed" for event in events)
    assert {
        "run_opportunity_scout",
        "run_business_research",
    } <= set(graph_event.metadata["node_path"])


def test_cli_airtable_context_infers_finance_expense_receipt_target(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "airtable_context_agent",
            "--json",
            "add a business expense to the airtable business expenses based on the receipt "
            "details which are: /tmp/example-business-cards-receipt.pdf",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    output = payload["output"]
    assert output["base_alias"] == "finance_tax_tracker"
    assert output["relevant_tables"] == ["Business Expenses"]
    assert "Total Expenses" in output["relevant_fields"]
    assert output["write_plan"]["operation"] == (
        "airtable_specialist_create_from_receipt_after_schema_and_approval"
    )
    assert output["write_plan"]["target"] == "finance_tax_tracker / Business Expenses"
    assert "receipt_local_path" in {
        item["key"] for item in output["write_plan"]["field_mapping"]
    }
    assert "Confirmed base/table/record identity" not in output["approval_needs"]
    assert "Confirmed expense-table schema field mapping" in output["approval_needs"]
    assert "not ask the operator to restate the base or table" in output["summary"]
    assert "Tax Payments" not in output["relevant_tables"]
    assert payload["send_enabled"] is False
    assert payload["side_effects"]["external_write_performed"] is False


def test_cli_airtable_context_resolves_selected_slack_receipt_path(
    capsys,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    receipt = tmp_path / "Receipt-example.pdf"
    receipt.write_bytes(b"%PDF-1.4\n% bounded test receipt\n")
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C_TEST",
                "thread_ts": "1783972624.677569",
                "request_ts": "1783972624.677569",
                "read_context": f"Slack attachment materialized locally: {receipt}",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "airtable_context_agent",
            "--context-file",
            str(context_file),
            "--json",
            "add this attached receipt as exactly one personal expense in Airtable",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    mapping = {
        item["key"]: item["value"]
        for item in payload["output"]["write_plan"]["field_mapping"]
    }
    assert mapping["receipt_local_path"] == str(receipt)
    assert payload["selected_agent"] == "airtable_context_agent"
    provider_context = cli._direct_airtable_receipt_provider_context(
        "airtable_context_agent",
        "add this attached receipt as exactly one personal expense in Airtable",
        execution_context={
            "schema": "keystone.direct_specialist_context.v1",
            "thread_transcript_tail": f"Slack attachment materialized locally: {receipt}",
        },
    )
    assert str(receipt) in provider_context
    assert "airtable_target_table" in provider_context


def test_airtable_context_dry_run_keeps_named_tracker_expense_target() -> None:
    output = cli._context_agent_dry_run_output(
        "airtable_context_agent",
        (
            "Create one synthetic Business Expenses record in the configured Finance & "
            "Tax Tracker and link the receipt https://example.test/receipt.pdf. Then "
            "update and remove only that marked test record."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    assert payload["base_alias"] == "finance_tax_tracker"
    assert payload["relevant_tables"] == ["Business Expenses"]
    assert payload["write_plan"]["target"] == (
        "finance_tax_tracker / Business Expenses"
    )
    assert "Tax Payments" not in payload["summary"]


def test_chief_calendar_fast_path_infers_defaults_without_model_or_graph(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            "on November 4th add an all day calendar event that Frontiers in Human "
            "Dynamics paper Due Date with note submit the final paper",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["calendar_action"]["start_date"].endswith("-11-04")
    assert payload["calendar_action"]["calendar_id"] == "primary"
    assert payload["calendar_action"]["timezone"] == "America/New_York"
    assert payload["calendar_action"]["description"] == "submit the final paper"
    assert payload["tool_receipt"]["status"] == "dry-run"
    assert payload["openai_requests"] == 0
    assert "manual_request_plan" not in payload


def test_named_cos_calendar_mention_uses_direct_path_without_model_or_graph(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--json",
            "@KNI",
            "CoS",
            "create a calendar event called KBA_TEST_CALENDAR_DIRECT on July 21, 2026",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["route"] == "chief_of_staff"
    assert payload["calendar_action"]["operation"] == "create"
    assert payload["calendar_action"]["title"] == "KBA_TEST_CALENDAR_DIRECT"
    assert payload["calendar_action"]["start_date"] == "2026-07-21"
    assert payload["tool_receipt"]["status"] == "dry-run"
    assert payload["openai_requests"] == 0
    assert "manual_request_plan" not in payload


def test_named_cos_dated_deadline_uses_calendar_path_without_calendar_keyword(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--json",
            "@KNI",
            "CoS",
            "add “UT AI Agents application due on July 23rd”",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "chief_of_staff"
    assert payload["calendar_action"]["operation"] == "create"
    assert payload["calendar_action"]["title"] == (
        "UT AI Agents application due on July 23rd"
    )
    assert payload["calendar_action"]["start_date"] == "2026-07-23"
    assert payload["calendar_action"]["all_day"] is True
    assert payload["tool_receipt"]["status"] == "dry-run"


def test_incidental_meeting_context_falls_through_to_shared_chief_planning(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "I have 30 seconds before a meeting. Using only these three facts, give me "
        "exactly three short bullets—what changed, why it matters, and what still "
        "needs live proof: negative constraints remove forbidden capabilities "
        "instead of blocking feasible work; a complete answer must not be labeled "
        "Need Input unless information is genuinely missing; the repaired offline "
        "boundary passes. Do not search, call tools or providers, draft messages, "
        "create or modify records, or include workflow or routing metadata."
    )

    def fail_calendar_interpretation(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Incidental meeting context must not enter Calendar interpretation.")

    monkeypatch.setattr(
        cli,
        "resolve_calendar_action_plan",
        fail_calendar_interpretation,
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "chief_of_staff"
    assert "calendar_action" not in payload
    assert payload["output"]["recommended_route"]["workflow_type"] == (
        "project-context-review"
    )


def test_calendar_provider_action_runs_only_after_shared_preflight(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    request = "create a calendar event called KBA_TEST_ORDER on July 21, 2026"

    def record_preflight(request_text: str, **kwargs: object) -> object:
        events.append("orchestrator_preflight")
        return _fake_orchestrator_preflight(request_text, **kwargs)

    original_resolver = cli.resolve_calendar_action_plan

    def record_calendar_resolution(*args: object, **kwargs: object) -> object:
        assert events == ["orchestrator_preflight"]
        events.append("calendar_action_interpreter")
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(cli, "run_orchestrator_preflight", record_preflight)
    monkeypatch.setattr(
        cli,
        "resolve_calendar_action_plan",
        record_calendar_resolution,
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert events == ["orchestrator_preflight", "calendar_action_interpreter"]
    assert payload["execution_admission"]["mode"] == "provider_action"
    assert payload["execution_admission"]["positive_intent"] is True
    assert payload["tool_receipt"]["status"] == "dry-run"


def test_provider_action_detector_cannot_veto_semantic_plan(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = "Using only these facts, return one short internal summary."

    def route_request_plan(request_text: str, **_kwargs: object) -> object:
        plan = infer_manual_request_plan(
            request_text,
            requested_agent="chief_of_staff",
        ).model_copy(
            update={
                "target_agent": "chief_of_staff",
                "intent": "route_request",
                "side_effect_policy": "draft_or_read_only",
            }
        )
        result = cli.route_request(request_text, manual_plan=plan)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent="chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            execution_allowed=True,
            manual_request_plan=plan,
            route_result=result,
        )

    monkeypatch.setattr(cli, "is_calendar_action_candidate", lambda _text: True)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", route_request_plan)
    monkeypatch.setattr(
        cli,
        "resolve_calendar_action_plan",
        lambda *_args, **_kwargs: pytest.fail(
            "Provider detector must not override the shared semantic plan."
        ),
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["intent"] == "route_request"
    assert "calendar_action" not in payload


def test_typed_calendar_executor_executes_complete_live_write_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create(title: str, start_date: str, **kwargs: object) -> dict[str, object]:
        captured.update({"title": title, "start_date": start_date, **kwargs})
        return {
            "status": "success",
            "operation": "create_calendar_event",
            "event_id": "kba-calendar-event",
            "title": title,
            "start_date": start_date,
            "html_link": "https://calendar.test/event",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "create_google_calendar_event_impl", fake_create)
    request = (
        "on November 4th add an all day calendar event that Frontiers in Human "
        "Dynamics paper Due Date"
    )
    plan = cli.infer_calendar_action_plan(request)
    assert plan is not None

    payload = cli.execute_direct_calendar_action(
        request,
        plan,
        live=True,
        openai_requests=0,
    )

    assert payload["status"] == "done"
    assert payload["openai_requests"] == 0
    assert payload["side_effects"]["calendar_write_performed"] is True
    assert captured["live"] is True
    assert str(captured["approval_reference"]).startswith("calendar-direct:")


def test_typed_calendar_executor_forwards_timed_event_fields_to_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create(title: str, start_date: str, **kwargs: object) -> dict[str, object]:
        captured.update({"title": title, "start_date": start_date, **kwargs})
        return {
            "status": "success",
            "operation": "create_calendar_event",
            "event_id": "kba-calendar-timed-event",
            "title": title,
            "start_date": start_date,
            "start_time": kwargs.get("start_time"),
            "html_link": "https://calendar.test/event",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "create_google_calendar_event_impl", fake_create)
    request = (
        "schedule a calendar meeting on July 14th, 2026 at 2pm titled "
        "Livestream with Corey Ching and Peter Steinberger"
    )
    plan = cli.infer_calendar_action_plan(request)
    assert plan is not None
    payload = cli.execute_direct_calendar_action(
        request,
        plan,
        live=True,
    )

    assert payload["status"] == "done"
    assert captured["start_time"] == "14:00"
    assert captured["end_time"] == "15:00"
    assert captured["timezone"] == "America/New_York"


def test_typed_calendar_executor_resolves_natural_update_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_reference": event_reference, "lookup": kwargs})
        return {
            "status": "success",
            "operation": "resolve_calendar_event",
            "event_reference": event_reference,
            "event_id": "kba-calendar-event",
            "title": "Frontiers in Human Dynamics paper Due Date",
            "start_date": "2026-11-04",
            "match_count": 1,
        }

    def fake_update(event_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_id": event_id, "update": kwargs})
        return {
            "status": "success",
            "operation": "update_calendar_event",
            "event_id": event_id,
            "title": "Frontiers in Human Dynamics paper Due Date",
            "start_date": "2026-11-04",
            "description_present": True,
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    monkeypatch.setattr(cli, "update_google_calendar_event_impl", fake_update)
    request = (
        "change the note on the Frontiers in Human Dynamics paper Due Date event "
        "to submit the final paper"
    )
    plan = cli.infer_calendar_action_plan(request)
    assert plan is not None
    payload = cli.execute_direct_calendar_action(
        request,
        plan,
        live=True,
    )

    assert payload["status"] == "done"
    assert payload["calendar_action"]["event_id"] == ""
    assert payload["calendar_action"]["event_reference"] == (
        "Frontiers in Human Dynamics paper Due Date"
    )
    assert payload["calendar_lookup"]["match_count"] == 1
    assert captured["event_id"] == "kba-calendar-event"
    assert captured["update"]["description"] == "submit the final paper"
    assert captured["update"]["live"] is True


def test_typed_calendar_executor_delete_resolves_exact_event_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_reference": event_reference, "lookup": kwargs})
        return {
            "status": "success",
            "operation": "resolve_calendar_event",
            "event_reference": event_reference,
            "event_id": "kba-calendar-delete-event",
            "title": "KBA_TEST_CALENDAR_DIRECT",
            "start_date": "2026-07-21",
            "match_count": 1,
        }

    def fake_delete(event_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_id": event_id, "delete": kwargs})
        return {
            "status": "success",
            "operation": "delete_calendar_event",
            "event_id": event_id,
            "title": "KBA_TEST_CALENDAR_DIRECT",
            "start_date": "2026-07-21",
            "verification": {
                "status": "verified_absent",
                "passed": True,
            },
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    monkeypatch.setattr(cli, "delete_google_calendar_event_impl", fake_delete)
    request = (
        "@KNI CoS remove the KBA_TEST_CALENDAR_DIRECT calendar event on July 21, 2026"
    )
    plan = cli.infer_calendar_action_plan(request)
    assert plan is not None
    payload = cli.execute_direct_calendar_action(
        request,
        plan,
        live=True,
    )

    assert payload["status"] == "done"
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["route"] == "chief_of_staff"
    assert payload["openai_requests"] == 0
    assert payload["calendar_action"]["operation"] == "delete"
    assert payload["calendar_lookup"]["match_count"] == 1
    assert payload["tool_receipt"]["verification"]["passed"] is True
    assert captured["event_id"] == "kba-calendar-delete-event"
    assert captured["delete"]["live"] is True


def test_typed_calendar_executor_thread_time_update_uses_prior_event_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_reference": event_reference, "lookup": kwargs})
        return {
            "status": "success",
            "operation": "resolve_calendar_event",
            "event_reference": event_reference,
            "event_id": "kba-calendar-thread-event",
            "title": "Livestream with Corey Ching and Peter Steinberger",
            "start_date": "2026-07-14",
            "match_count": 1,
        }

    def fake_update(event_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_id": event_id, "update": kwargs})
        return {
            "status": "success",
            "operation": "update_calendar_event",
            "event_id": event_id,
            "title": "Livestream with Corey Ching and Peter Steinberger",
            "start_date": "2026-07-14",
            "start_time": "14:00",
            "all_day": False,
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    monkeypatch.setattr(cli, "update_google_calendar_event_impl", fake_update)
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS schedule a calendar meeting on July 14th at 2pm titled "
        "\u201cLivestream with Corey Ching and Peter Steinberger\u201d "
        "Previous result title: Previous result: not available "
        "User follow-up: Move this event from all-day to 2pm-3pm. "
        "Continue the same agent task."
    )

    plan = CalendarActionPlan(
        operation="update",
        event_reference="Livestream with Corey Ching and Peter Steinberger",
        event_reference_date="2026-07-14",
        start_time="14:00",
        end_time="15:00",
        all_day=False,
        complete=True,
    )
    payload = cli.execute_direct_calendar_action(request, plan, live=True)
    assert payload["status"] == "done"
    assert captured["event_reference"] == (
        "Livestream with Corey Ching and Peter Steinberger"
    )
    assert captured["lookup"]["start_date"] == "2026-07-14"
    assert captured["update"]["start_date"] == "2026-07-14"
    assert captured["update"]["start_time"] == "14:00"
    assert captured["update"]["end_time"] == "15:00"


def test_typed_calendar_executor_thread_note_appends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    url = "https://example.test/thread/123"
    plan = CalendarActionPlan(
        operation="update",
        description=url,
        append_description=True,
        event_reference="Partner livestream",
        event_reference_date="2026-07-14",
        all_day=False,
        complete=True,
    )

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": "success",
            "event_reference": event_reference,
            "event_id": "kba-calendar-note-event",
            "title": "Partner livestream",
            "start_date": "2026-07-14",
            "match_count": 1,
        }

    def fake_update(event_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_id": event_id, "update": kwargs})
        return {
            "status": "success",
            "operation": "update_calendar_event",
            "event_id": event_id,
            "title": "Partner livestream",
            "start_date": "2026-07-14",
            "description_present": True,
            "description_mode": "append",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    monkeypatch.setattr(cli, "update_google_calendar_event_impl", fake_update)
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: schedule a calendar event on July 14 titled Partner livestream "
        "Previous result title: Calendar event updated "
        "Previous result: provider verification passed "
        f"User follow-up: Add this link to the notes: {url} "
        "Continue the same agent task."
    )

    payload = cli.execute_direct_calendar_action(request, plan, live=True)
    assert payload["status"] == "done"
    assert captured["update"]["description"] == url
    assert captured["update"]["append_description"] is True


def test_typed_calendar_executor_verifies_event_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Current user request (authoritative): Is it on the calendar now? "
        "Provider affinity: calendar "
        "Previous request: CoS add UT Austin Course Starts on August 15, 2026 "
        "to my Google Calendar. Can add it at 8am-9am. "
        "Previous result title: Business Agents WorkItem Failed "
        "Previous result: No specialist or provider action ran. "
        "User follow-up: Is it on the calendar now? "
        "Continue the same agent task."
    )
    captured: dict[str, object] = {}

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        captured["event_reference"] = event_reference
        captured["lookup"] = kwargs
        return {
            "status": "success",
            "operation": "resolve_calendar_event",
            "event_reference": event_reference,
            "event_id": "calendar-event-1",
            "title": "UT Austin Course Starts",
            "start_date": "2026-08-15",
            "match_count": 1,
            "provider_link": "https://calendar.test/event",
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    plan = CalendarActionPlan(
        operation="read",
        event_reference="UT Austin Course Starts",
        start_date="2026-08-15",
        complete=True,
    )
    payload = cli.execute_direct_calendar_action(request, plan, live=True)
    assert captured["event_reference"] == "UT Austin Course Starts"
    assert captured["lookup"]["start_date"] == "2026-08-15"
    assert payload["status"] == "done"
    assert payload["side_effects"]["calendar_write_performed"] is False
    assert payload["tool_receipt"]["found"] is True
    assert payload["public_result"]["status"] == "completed"
    assert payload["public_result"]["text"] == (
        'Yes - "UT Austin Course Starts" is on your Google Calendar on 2026-08-15.'
    )


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "@KNI CoS add UT Austin Course Starts on August 15, 2026 to google "
            "calendar. Can add it at 8am-9am."
        ),
        (
            "@KNI CoS add UT Austin Course Starts on August 15, 2026 to my Google "
            "Calendar from 8am to 9am."
        ),
        (
            "@KNI Chief of Staff, please put UT Austin Course Starts on my Google "
            "Calendar for August 15, 2026, 8:00-9:00 AM."
        ),
    ],
)
def test_live_calendar_variants_use_shared_chief_provider_path_with_five_call_ceiling(
    request_text: str,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    child_envs: list[dict[str, str]] = []

    def fail_direct_calendar_path(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Live natural-language Calendar asks must use the shared Chief path.")

    def fake_semantic_preflight(
        preflight_request: str,
        *,
        requested_agent: str | None = None,
        **_kwargs: object,
    ) -> object:
        plan = infer_manual_request_plan(
            preflight_request,
            requested_agent=requested_agent,
        ).model_copy(
            update={
                "source": "llm",
                "target_agent": "chief_of_staff",
                "intent": "business_system_write",
                "target_type": "business_system_context",
                "provider_system": "google_calendar",
                "primary_target": "UT Austin Course Starts",
                "requires_live_search": False,
            }
        )
        result = cli.route_request(preflight_request, manual_plan=plan)
        return cli.OrchestratorPreflight(
            request_text=preflight_request,
            requested_agent=requested_agent or "chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            execution_allowed=True,
            manual_request_plan=plan,
            route_result=result,
        )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        child_envs.append(dict(kwargs.get("env") or {}))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "done",
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "human_summary": (
                        'Google Calendar event created and verified: '
                        '"UT Austin Course Starts" on 2026-08-15.'
                    ),
                    "output": {
                        "summary": (
                            'Google Calendar event created and verified: '
                            '"UT Austin Course Starts" on 2026-08-15.'
                        )
                    },
                    "tool_receipts": [
                        {
                            "status": "success",
                            "operation": "create_calendar_event",
                            "title": "UT Austin Course Starts",
                            "start_date": "2026-08-15",
                            "verification": {"passed": True},
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_semantic_preflight)
    monkeypatch.setattr(cli, "resolve_calendar_action_plan", fail_direct_calendar_path)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "5",
            "--json",
            request_text,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload.get("status") != "blocked"
    assert payload["script_payload"]["status"] == "done"
    assert payload["manual_request_plan"]["provider_system"] == "google_calendar"
    assert calls and "scripts/run_chief_of_staff.py" in calls[0]
    assert "--live-search" not in calls[0]
    assert "--live-search-plan" not in calls[0]
    assert calls[0][calls[0].index("--quality") + 1] == "fast"
    child_plan = json.loads(child_envs[0][MANUAL_REQUEST_PLAN_ENV])
    assert child_plan["provider_system"] == "google_calendar"
    assert child_plan["intent"] == "business_system_write"


def test_live_calendar_followup_reuses_human_root_without_failed_bot_output_or_search(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = "\n".join(
        [
            "chief of staff continue this prior Slack thread.",
            "Current user request (authoritative): Is it on the calendar now?",
            "Provider affinity: calendar",
            (
                "Previous request: CoS add UT Austin Course Starts on August 15, 2026 "
                "to my Google Calendar. Can add it at 8am-9am."
            ),
            "Previous result title: Business Agents WorkItem Failed",
            "Previous result: No specialist or provider action ran.",
            "User follow-up: Is it on the calendar now?",
            "Continue the same agent task.",
        ]
    )
    captured: dict[str, object] = {}

    def fake_preflight(request_text: str, **kwargs: object) -> object:
        captured["preflight_request"] = request_text
        captured["workflow_state"] = kwargs.get("workflow_state")
        plan = infer_manual_request_plan(
            request_text,
            requested_agent="chief_of_staff",
        ).model_copy(
            update={
                "source": "llm",
                "target_agent": "chief_of_staff",
                "intent": "context_lookup",
                "target_type": "business_system_context",
                "provider_system": "google_calendar",
                "primary_target": "UT Austin Course Starts",
                "requires_live_search": False,
            }
        )
        result = cli.route_request(request_text, manual_plan=plan)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent="chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            execution_allowed=True,
            manual_request_plan=plan,
            route_result=result,
        )

    def fail_direct_calendar_path(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Live provider follow-ups must use the shared Chief path.")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["child_input"] = command[command.index("--input") + 1]
        captured["child_env"] = dict(kwargs.get("env") or {})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "done",
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "human_summary": (
                        'Yes - "UT Austin Course Starts" is on your Google Calendar '
                        "on 2026-08-15."
                    ),
                    "output": {
                        "summary": (
                            'Yes - "UT Austin Course Starts" is on your Google Calendar '
                            "on 2026-08-15."
                        )
                    },
                    "tool_receipts": [
                        {
                            "status": "success",
                            "operation": "read_calendar_window",
                            "events": [
                                {
                                    "event_id": "event-1",
                                    "title": "UT Austin Course Starts",
                                    "start": "2026-08-15T08:00:00-04:00",
                                }
                            ],
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "resolve_calendar_action_plan", fail_direct_calendar_path)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--max-openai-requests",
            "5",
            "--json",
            envelope,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("status") != "blocked"
    assert payload["script_payload"]["status"] == "done"
    assert captured["preflight_request"] == "Is it on the calendar now?"
    workflow_state = captured["workflow_state"]
    assert isinstance(workflow_state, dict)
    assert workflow_state["execution_continuation"]["provider_affinity"] == "calendar"
    child_input = str(captured["child_input"])
    assert "UT Austin Course Starts" in child_input
    assert "Authoritative follow-up: Is it on the calendar now?" in child_input
    assert "No specialist or provider action ran" not in child_input
    assert "Business Agents WorkItem Failed" not in child_input
    command = captured["command"]
    assert isinstance(command, list)
    assert "--live-search" not in command
    assert "--live-search-plan" not in command
    assert command[command.index("--quality") + 1] == "fast"


def test_live_unowned_calendar_followup_executes_typed_action_before_work_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    envelope = "\n".join(
        [
            "business agents continue this prior Slack thread.",
            (
                "Current user request (authoritative): "
                "Move this event to 4:10 PM and make it 15 minutes."
            ),
            "Linked WorkItem: wi_stale_calendar_plan",
            "Provider affinity: calendar",
            "Previous request: Move this event to 4:20 PM and make it 25 minutes.",
            "Previous result title: Calendar update plan",
            "Previous result: This response reflects the plan, not a live mutation.",
            "User follow-up: Move this event to 4:10 PM and make it 15 minutes.",
            "Continue the same agent task.",
        ]
    )
    context_file = tmp_path / "slack-calendar-followup.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.message_context.v1",
                "thread_ts": "1784574080.467439",
                "thread_root_request": (
                    "CoS: Put a 15-minute event called "
                    '"KBA_TEST_CALENDAR natural language canary" on my calendar '
                    "for July 22, 2026 at 4:10 PM Eastern."
                ),
            }
        ),
        encoding="utf-8",
    )
    plan = CalendarActionPlan(
        operation="update",
        event_reference="KBA_TEST_CALENDAR natural language canary",
        event_reference_date="2026-07-22",
        start_date="2026-07-22",
        start_time="16:10",
        end_time="16:25",
        all_day=False,
        complete=True,
    )
    captured: dict[str, object] = {}

    def fake_preflight(request_text: str, **_kwargs: object) -> object:
        semantic_plan = ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["update"],
            primary_target="KBA_TEST_CALENDAR natural language canary",
            required_entities=["KBA_TEST_CALENDAR natural language canary"],
            requires_live_search=False,
            requires_durable_state=False,
            objective=request_text,
        )
        return cli.OrchestratorPreflight(
            request_text=request_text,
            advisory_only=True,
            selected_agent="chief_of_staff",
            execution_allowed=True,
            manual_request_plan=semantic_plan,
            route_result=cli.route_request(request_text, manual_plan=semantic_plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_resolve(
        request_text: str,
        fallback: object,
        **kwargs: object,
    ) -> object:
        captured["resolve_request"] = request_text
        captured["fallback"] = fallback
        captured["resolve_kwargs"] = kwargs
        return SimpleNamespace(
            plan=plan,
            interpreter_used=True,
            openai_requests=1,
            warnings=(),
        )

    def fake_direct(
        input_text: str,
        resolved_plan: CalendarActionPlan,
        **kwargs: object,
    ) -> int:
        captured["direct_input"] = input_text
        captured["resolved_plan"] = resolved_plan
        captured["direct_kwargs"] = kwargs
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "resolve_calendar_action_plan", fake_resolve)
    monkeypatch.setattr(cli, "run_direct_calendar_action", fake_direct)
    monkeypatch.setattr(
        cli,
        "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail(
            "A one-owner live Calendar follow-up must not become a WorkItem."
        ),
    )

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--context-file",
            str(context_file),
            "--max-openai-requests",
            "5",
            "--json",
            envelope,
        ]
    )

    assert exit_code == 0
    assert captured["resolved_plan"] == plan
    assert captured["resolve_kwargs"]["live"] is True
    assert captured["resolve_kwargs"]["semantic_candidate"] is True
    fallback = captured["fallback"]
    assert isinstance(fallback, CalendarActionPlan)
    assert fallback.event_reference == "KBA_TEST_CALENDAR natural language canary"
    assert fallback.event_reference_date == "2026-07-22"
    direct_input = str(captured["direct_input"])
    assert "KBA_TEST_CALENDAR natural language canary" in direct_input
    assert "Move this event to 4:10 PM" in direct_input
    assert "Linked WorkItem: wi_stale_calendar_plan" not in direct_input
    assert captured["direct_kwargs"]["live"] is True
    assert captured["direct_kwargs"]["openai_requests"] == 2


def test_continuation_provider_affinity_fills_only_unspecified_provider() -> None:
    plan = infer_manual_request_plan(
        "What date is the UT Course Orientation Session?",
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "provider_system": "unspecified",
        }
    )
    preflight = cli.OrchestratorPreflight(
        request_text="What date is the UT Course Orientation Session?",
        requested_agent="chief_of_staff",
        advisory_only=True,
        selected_agent="chief_of_staff",
        execution_allowed=True,
        manual_request_plan=plan,
        route_result=cli.route_request(
            "What date is the UT Course Orientation Session?",
            manual_plan=plan,
        ),
    )

    resolved = cli._apply_continuation_provider_affinity(preflight, "calendar")

    assert resolved.manual_request_plan.provider_system == "google_calendar"
    assert resolved.manual_request_plan.target_agent == "chief_of_staff"
    assert resolved.manual_request_plan.intent == "context_lookup"

    explicit_gmail = preflight.model_copy(
        update={
            "manual_request_plan": plan.model_copy(
                update={"provider_system": "gmail"}
            )
        }
    )
    unchanged = cli._apply_continuation_provider_affinity(
        explicit_gmail,
        "calendar",
    )
    assert unchanged.manual_request_plan.provider_system == "gmail"


def test_bounded_provider_plan_uses_provider_budget_not_manager_graph_budget() -> None:
    request = "What date is the UT Course Orientation Session?"
    plan = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "provider_system": "google_calendar",
            "requires_live_search": False,
            "workflow": [],
        }
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "google_calendar_bounded_provider_sdk",
    ]


def test_live_provider_plan_cannot_be_downgraded_to_tool_free_response() -> None:
    request = (
        'Using only the relevant message, find the Gmail email titled "Example" '
        "and tell me what it says."
    )
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        provider_system="gmail",
        provider_operations=["read"],
        primary_target="Example",
        target_type="gmail_thread",
    )

    assert (
        cli._should_run_direct_supplied_response(
            request,
            requested_route="gmail_triage",
            manual_plan=plan,
        )
        is False
    )


def test_live_local_attachment_plan_does_not_admit_unneeded_provider_tools() -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        provider_system="airtable",
        provider_operations=["read"],
        primary_target="receipt details from attached image",
        target_type="local_document_collection",
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            source_type_preference=["attached local file image"],
            permission_state="read_only",
        ),
    )

    assert (
        cli._should_run_direct_supplied_response(
            "Read the attached receipt and report four fields.",
            requested_route="chief_of_staff",
            manual_plan=plan,
        )
        is True
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "Turn these supplied facts into a concise answer.",
        "Please synthesize the material I pasted into a short note.",
        "What is the clearest takeaway from the information above?",
    ],
)
def test_live_provider_free_plan_uses_same_tool_free_path_across_phrasings(
    request_text: str,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="business_research_analyst",
        intent="route_request",
        task_objective="route_or_continue",
        provider_system="unspecified",
        provider_operations=[],
        requires_live_search=False,
        requires_durable_state=False,
        requires_approved_context=False,
    )

    assert (
        cli._should_run_direct_supplied_response(
            request_text,
            requested_route="business_research_analyst",
            manual_plan=plan,
        )
        is True
    )


def test_calendar_followup_planner_fallback_dispatches_chief_within_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context_file = tmp_path / "slack-calendar-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.message_context.v1",
                "team_id": "T123",
                "channel_id": "C123",
                "thread_ts": "1784563046.670369",
                "request_ts": "1784567539.798959",
                "read_context": "Calendar thread context.",
            }
        ),
        encoding="utf-8",
    )
    request = "\n".join(
        [
            "business agents continue this prior Slack thread.",
            (
                "Current user request (authoritative): "
                "What date is the UT Course Orientation Session?"
            ),
            "Provider affinity: calendar",
            "Previous request: Add the UT Course Orientation Session to my calendar.",
            "Previous result title: Business Agents WorkItem Failed",
            "Previous result: The prior run failed before provider execution.",
            "User follow-up: What date is the UT Course Orientation Session?",
            "Continue the same agent task.",
        ]
    )
    original_preflight = cli.run_orchestrator_preflight
    dispatched: dict[str, object] = {}

    def fallback_preflight(request_text, **kwargs):
        kwargs["live_manual_plan"] = False
        return original_preflight(request_text, **kwargs)

    def capture_specialist(route, input_text, **kwargs):
        dispatched.update(
            {
                "route": route,
                "input_text": input_text,
                "manual_plan": kwargs["manual_plan"],
            }
        )
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fallback_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", capture_specialist)

    exit_code = main(
        [
            "ask",
            "--database-url",
            f"sqlite:///{tmp_path / 'fallback.db'}",
            "--context-file",
            str(context_file),
            "--live-sdk",
            "--max-openai-requests",
            "8",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert dispatched["route"] == "chief_of_staff"
    plan = dispatched["manual_plan"]
    assert isinstance(plan, ManualRequestPlan)
    assert plan.intent == "context_lookup"
    assert plan.provider_system == "google_calendar"
    assert plan.workflow == []


def test_chief_calendar_natural_update_dry_run_previews_lookup_without_write(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            "change the note on the Frontiers paper due date event to submit the final paper",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry-run"
    assert payload["calendar_lookup"]["status"] == "dry-run"
    assert payload["calendar_lookup"]["event_reference"] == "Frontiers paper due date"
    assert payload["side_effects"]["calendar_write_performed"] is False
    assert payload["openai_requests"] == 0


def test_cli_no_live_chief_receipt_command_returns_direct_airtable_write_plan(
    capsys,
    monkeypatch,
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "example-business-cards-receipt.pdf"
    receipt_path.write_bytes(b"%PDF-1.4\nreceipt fixture")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'ask.db'}",
            "@KNI",
            "chief",
            "of",
            "staff",
            "add",
            "a",
            "business",
            "expense",
            "to",
            "the",
            "airtable",
            "business",
            "expenses",
            "based",
            "on",
            "the",
            "receipt",
            "details",
            "which",
            "are:",
            str(receipt_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    human_summary = payload["human_summary"]
    assert exit_code == 0
    assert payload["status"] == "done"
    assert payload["route"] == "airtable_context_agent"
    assert payload["manual_request_plan"]["intent"] == "business_system_write"
    assert payload["manual_request_plan"]["target_type"] == "business_system_context"
    assert "Read-only only" not in human_summary
    assert "finance_tax_tracker" in human_summary
    assert "Business Expenses" in human_summary
    assert payload["output"]["write_plan"]["operation"] == (
        "airtable_specialist_create_from_receipt_after_schema_and_approval"
    )
    assert payload["output"]["write_plan"]["target"] == (
        "finance_tax_tracker / Business Expenses"
    )
    assert payload["side_effects"]["external_write_performed"] is False


def test_cli_ask_chief_of_staff_outputs_deterministic_plan(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "see my email and send an update to the #onboarding channel",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "gmail-summary"
    assert payload["output"]["recommended_route"]["target_channel"] == "onboarding"
    assert payload["output"]["slack_post_allowed"] is False


def test_cli_ask_chief_of_staff_dry_run_uses_orchestrator_manual_plan(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeRoute:
        workflow_type = "chief_of_staff"
        command_text = "@KNI chief of staff review"
        target_channel = ""

    class FakeChiefResult:
        summary = "Chief of Staff used parent Orchestrator plan."
        recommended_route = FakeRoute()
        slack_post_allowed = False

        def model_dump(self, **_kwargs):
            return {
                "summary": self.summary,
                "recommended_route": {
                    "workflow_type": self.recommended_route.workflow_type,
                    "command_text": self.recommended_route.command_text,
                    "target_channel": self.recommended_route.target_channel,
                },
                "slack_post_allowed": self.slack_post_allowed,
            }

    def fake_plan_chief_of_staff_request(*_args, **kwargs):
        captured["manual_request_plan"] = kwargs.get("manual_request_plan")
        return FakeChiefResult()

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "plan_chief_of_staff_request", fake_plan_chief_of_staff_request)

    exit_code = main(
        [
            "ask",
            "--json",
            "--agent",
            "chief_of_staff",
            "review",
            "the",
            "state",
            "of",
            "KNI",
            "2026",
            "and",
            "why",
            "the",
            "prior",
            "answer",
            "was",
            "unrelated",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manual_plan = captured["manual_request_plan"]

    assert exit_code == 0
    assert manual_plan is not None
    assert manual_plan.target_agent == "chief_of_staff"
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["output"]["summary"] == "Chief of Staff used parent Orchestrator plan."


def test_cli_ask_kni_chief_of_staff_uses_chief_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-chief.db'}"

    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "chief",
            "of",
            "staff",
            "summarize",
            "open",
            "Slack",
            "follow-ups",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: chief_of_staff" in output
    assert "Artifacts: chief_of_staff_plan:" in output


def test_cli_ask_kni_chief_natural_research_outreach_uses_graph_workflow(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-chief-research-outreach.db'}"
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    exit_code = main(
        [
            "ask",
            "--json",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "--max-manager-steps",
            "4",
            "@KNI",
            "chief",
            "of",
            "staff",
            "NeuroFlow",
            "has",
            "been",
            "coming",
            "up",
            "as",
            "a",
            "behavioral-health",
            "AI",
            "company",
            "with",
            "payer",
            "partnership",
            "and",
            "outcomes-evidence",
            "signals.",
            "Do",
            "research,",
            "assess",
            "whether",
            "this",
            "is",
            "a",
            "real",
            "KNI",
            "advisory/research",
            "opportunity,",
            "identify",
            "what",
            "source-backed",
            "evidence",
            "is",
            "still",
            "missing,",
            "and",
            "decide",
            "whether",
            "it",
            "should",
            "stop",
            "at",
            "an",
            "approval",
            "checkpoint",
            "before",
            "any",
            "outreach.",
            "If",
            "the",
            "evidence",
            "supports",
            "pursuing",
            "it,",
            "include",
            "a",
            "draft-only",
            "Slack-thread",
            "sample",
            "outreach",
            "for",
            "review.",
            "Do",
            "not",
            "send",
            "email,",
            "create",
            "Gmail",
            "drafts,",
            "post",
            "outside",
            "this",
            "thread,",
            "schedule,",
            "publish,",
            "or",
            "write",
            "external",
            "systems.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    artifact_types = {
        artifact["artifact_type"] for artifact in payload["work_item"]["artifact_refs"]
    }
    blocker_codes = {blocker["code"] for blocker in payload["blockers"]}
    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")

    assert payload["route"] == "outreach_composer"
    assert payload["status"] == "blocked"
    assert {"company_profile", "opportunity"} <= artifact_types
    assert "outreach_draft" not in artifact_types
    assert "outreach_draft_needs_model_reasoning" in blocker_codes
    assert graph_event.metadata["checkpoint_required"] is False
    assert "run_business_research" in graph_event.metadata["node_path"]
    assert "run_opportunity_scout" in graph_event.metadata["node_path"]
    assert "run_outreach_composer" in graph_event.metadata["node_path"]


def test_cli_ask_chief_of_staff_reference_capture_persists_memory(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "--database-url",
            database_url,
            "keep this for future reference:",
            "NIH AI conference with virtual attendees:",
            url,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "reference-capture"
    assert payload["output"]["summary"].startswith("Saved reference for future use:")

    memories = SQLiteStore(database_url).retrieve_memory(
        "NIH AI conference",
        memory_types=["operator_reference"],
    )
    assert len(memories) == 1
    assert memories[0].content["url"] == url


def test_cli_ask_agent_override_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Output type: ResearchBrief" in output
    assert "Orchestrator review:" in output
    assert calls
    assert "scripts/run_company_research.py" in calls[0]
    assert "--live-sdk" in calls[0]
    assert "--live-search-plan" in calls[0]
    assert "--compact-instructions" in calls[0]


def test_cli_live_finance_receipt_write_uses_shared_live_manual_planner(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_specialist(route, input_text, **kwargs):
        captured["route"] = route
        captured["input_text"] = input_text
        captured["manual_plan"] = kwargs["manual_plan"]
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_specialist)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--json",
            "@KNI",
            "chief",
            "of",
            "staff",
            "add",
            "a",
            "business",
            "expense",
            "to",
            "the",
            "airtable",
            "business",
            "expenses",
            "based",
            "on",
            "the",
            "receipt",
            "details",
            "which",
            "are:",
            "/tmp/example-business-cards-receipt.pdf",
        ]
    )

    assert exit_code == 0
    assert captured["live_manual_plan"] is True
    assert captured["route"] == "airtable_context_agent"
    assert "example-business-cards-receipt.pdf" in str(captured["input_text"])
    plan = captured["manual_plan"]
    assert plan.intent == "business_system_write"
    assert plan.target_type == "business_system_context"


def test_cli_bounded_smoke_wording_keeps_live_manual_planner_and_suppresses_search(
    monkeypatch,
    capsys,
) -> None:
    prompt = (
        "Research smoke: research NeuroFlow for a short internal read-only company note. "
        "Stay on Business Research only; do not scout opportunities or draft outreach. "
        "Live SDK is approved only for this bounded read-only smoke if the backend would "
        "normally use it; live web search is not approved. Use local/dry-run retrieval "
        "where possible. Do not send email, create drafts, post elsewhere, publish, "
        "schedule, or write external systems."
    )
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_direct(route, input_text, **kwargs):
        captured["direct"] = {"route": route, "input_text": input_text, **kwargs}
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_direct)

    exit_code = main(["ask", "--live-search", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert captured["live_manual_plan"] is True
    assert cli._request_forbids_live_research(prompt) is True
    direct = captured["direct"]
    assert direct["route"] == "business_research_analyst"
    assert direct["manual_plan"].requires_live_search is False


def test_cli_ask_agent_override_promotes_child_human_summary(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    summary = (
        "*Answer:*\nCorti has a clean source-backed fit summary.\n\n"
        "*Detailed Summary:*\n* Product/workflow: clinical AI.\n\n"
        "*Useful references:*\n* Corti: https://www.corti.ai"
    )

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "CompanyResearchFocusedBrief",
                    "send_enabled": False,
                    "human_summary": summary,
                    "output": {"company_name": "Corti", "send_enabled": False},
                    "model": {"provider": "openai", "name": "gpt-5.4-mini"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'runs.db'}",
            "research",
            "Corti",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == summary
    assert payload["slack_display_text"] == summary
    assert payload["display_text"] == summary
    assert payload["summary"] == summary
    assert payload["script_payload"]["human_summary"] == summary
    assert payload["output_type"] == "CompanyResearchFocusedBrief"


def test_cli_live_payload_text_mode_prefers_human_summary_over_message(
    capsys,
) -> None:
    summary = (
        "*Answer:*\nSuki has a clean answer-first fit check.\n\n"
        "*Detailed Summary:*\nDiagnostics should not render before this text."
    )

    assert (
        cli._print_ask_live_payload(
            {
                "agent_name": "Business Research Analyst",
                "output_type": "CompanyResearchFocusedBrief",
                "send_enabled": False,
                "message": "Business Agents Company Research Brief Ready",
                "human_summary": summary,
            },
            json_output=False,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Business Agents Company Research Brief Ready" not in output
    assert summary in output
    if "Orchestrator review:" in output:
        assert output.index("*Answer:*") < output.index("Orchestrator review:")


def test_cli_live_outreach_inline_context_uses_work_item_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'outreach-inline.db'}"

    def fake_run_ask_work_item(input_text, **kwargs):
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_work_item", fake_run_ask_work_item)

    exit_code = cli._run_ask_outreach_composer_live(
        "outreach composer agent: diagnostic case diag_outreach flexible labels. "
        "Prepare a draft-only email paragraph. Target contact: Alex Rivera at "
        "Example Health. Approved evidence: Example Health asked whether Keystone "
        "could review its remote patient monitoring AI validation workflow. "
        "Do not send email or create a Gmail draft.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["database_url"] == database_url
    assert captured["live_sdk"] is True
    assert captured["live_search"] is False
    assert captured["cost_tracking_requested"] is True
    assert "Approved evidence" in str(captured["input_text"])


def test_cli_live_opportunity_scout_no_external_context_uses_direct_inline_synthesis(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'opportunity-inline.db'}"

    def fake_run_ask_script_live(route, input_text, command, **kwargs):
        captured["route"] = route
        captured["input_text"] = input_text
        captured["command"] = command
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_script_live", fake_run_ask_script_live)

    exit_code = cli._run_ask_opportunity_scout_live(
        "opportunity scout agent: diagnostic case diag_opp. Use only this sanitized "
        "inline context and do not research externally: Cedar Grove Pediatrics is "
        "considering whether Keystone could review a measurement dashboard before "
        "an internal pilot. Scout two practical opportunity directions.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["route"] == "opportunity_scout"
    assert captured["database_url"] == database_url
    assert captured["cost_tracking_requested"] is True
    assert "Cedar Grove Pediatrics" in str(captured["input_text"])
    command = captured["command"]
    assert "scripts/run_compact_opportunity_assessment.py" in command
    assert "--inline-source-context" in command
    assert "--no-save-output" in command
    assert command[command.index("--max-openai-requests") + 1] == "1"


def test_cli_live_business_research_no_external_context_uses_direct_inline_synthesis(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'business-research-inline.db'}"

    def fake_run_ask_script_live(route, input_text, command, **kwargs):
        captured["route"] = route
        captured["input_text"] = input_text
        captured["command"] = command
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_script_live", fake_run_ask_script_live)

    exit_code = cli._run_ask_company_research_live(
        "business research analyst agent: diagnostic case diag_bra. Use only this "
        "sanitized inline context and do not research externally: Northstar Sleep Lab "
        "is considering whether Keystone could review an internal sleep-study "
        "operations dashboard before a November leadership review. Return a concise "
        "internal research handoff.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["route"] == "business_research_analyst"
    assert captured["database_url"] == database_url
    assert captured["cost_tracking_requested"] is True
    assert "Northstar Sleep Lab" in str(captured["input_text"])
    command = captured["command"]
    assert "--inline-source-context" in command
    assert command[command.index("--inline-source-context") + 1] == captured["input_text"]
    assert "--no-live-search" in command
    assert "--compact-instructions" in command
    assert "--focused-brief" in command


def test_company_research_inline_context_is_one_user_provided_source() -> None:
    import scripts.run_company_research as company_cli

    evidence = "Northstar Sleep Lab is considering an internal dashboard review."
    args = company_cli.build_parser().parse_args(
        [
            "--company",
            "Northstar Sleep Lab",
            "--inline-source-context",
            evidence,
            "--no-live-search",
        ]
    )

    profile, metadata = company_cli._retrieve_company_profile(args)

    assert profile.name == "Northstar Sleep Lab"
    assert profile.description == evidence
    assert profile.evidence == [evidence]
    assert len(profile.sources) == 1
    assert profile.sources[0].source_type == "user_provided"
    assert profile.sources[0].url == "operator://inline-company-context"
    assert metadata["mode"] == "inline_context"
    assert metadata["live_search"] is False


def test_company_research_quick_retrieval_receives_raw_operator_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as company_cli

    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(**kwargs: object):
        captured.update(kwargs)
        return company_cli.CompanyProfile(name="Abridge"), {"mode": "live_search"}

    monkeypatch.setattr(
        company_cli,
        "retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(
        company_cli,
        "require_cli_live_confirmation",
        lambda **_kwargs: None,
    )
    request_text = "Who is Abridge? Summarize the company in 20 words."
    args = company_cli.build_parser().parse_args(
        [
            "--company",
            "Abridge",
            "--request-text",
            request_text,
            "--live-search",
            "--no-dry-run",
            "--quick-retrieval",
        ]
    )

    company_cli._retrieve_company_profile(args)

    assert captured["request_text"] == request_text
    assert captured["max_results"] == 5
    assert captured["agents_web_search_max_calls"] == 1
    assert captured["tavily_search_fallback"] is False
    assert captured["exa_search_fallback"] is False
    assert captured["extract_selected_pages"] is False
    assert captured["max_queries"] == 2


def test_cli_ask_context_agent_override_live_sdk_runs_typed_agent(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    database_url = f"sqlite:///{tmp_path / 'context-agent.db'}"

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "airtable_context_agent"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run_typed_sdk_sync(agent, prompt, output_type, **kwargs):
        calls.append(
            {
                "agent_name": agent.name,
                "prompt": prompt,
                "output_type": output_type.__name__,
                "live": kwargs.get("live"),
                "live_reads_env": os.environ.get(cli.AIRTABLE_LIVE_READS_ENV),
                "operator_approval": os.environ.get(
                    "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
                ),
                "max_turns": kwargs.get("max_turns"),
            }
        )
        return (
            SimpleNamespace(
                final_output=None,
                usage=None,
                context_wrapper=SimpleNamespace(
                    usage=SimpleNamespace(
                        requests=2,
                        input_tokens=1000,
                        output_tokens=100,
                        total_tokens=1100,
                        input_tokens_details=SimpleNamespace(cached_tokens=200),
                        output_tokens_details=SimpleNamespace(reasoning_tokens=20),
                    )
                ),
                new_items=[],
            ),
            cli.AirtableContextResult(
                mode="llm",
                summary="Schema available for Finance & Tax Tracker.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Business Income"],
                relevant_fields=["Date", "Amount", "Client"],
                recommended_actions=["Read at most one Business Income record."],
            ),
        )

    monkeypatch.delenv(cli.AIRTABLE_LIVE_READS_ENV, raising=False)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)

    exit_code = main(
        [
            "ask",
            "--agent",
            "airtable_context_agent",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            "Read-only context test for the 2026 Finance & Tax Tracker.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "live_sdk"
    assert payload["selected_agent"] == "airtable_context_agent"
    assert payload["output_type"] == "AirtableContextResult"
    assert payload["output"]["relevant_tables"] == ["Business Income"]
    assert payload["send_enabled"] is False
    assert payload["model_execution"] == {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "run_mode": "live_sdk",
        "usage_available": True,
        "cost_available": True,
        "base_url_configured": False,
        "gateway_mode": False,
        "sdk_turn_policy": {
            "agent_name": "airtable_context_agent",
            "max_turns": 2,
            "source": "direct_single_action:fixed_default",
        },
    }
    assert payload["usage"] == {
        "available": True,
        "requests": 2,
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "cached_input_tokens": 200,
        "reasoning_output_tokens": 20,
        "cache_hit_rate": 0.2,
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
    }
    assert payload["cost"]["estimated_usd"] > 0
    assert isinstance(payload["agent_run_id"], int)
    assert calls == [
        {
            "agent_name": "airtable_context_agent",
            "prompt": "Read-only context test for the 2026 Finance & Tax Tracker.",
            "output_type": "AirtableContextResult",
            "live": True,
            "live_reads_env": "true",
            "operator_approval": None,
            "max_turns": 2,
        }
    ]
    assert os.environ.get(cli.AIRTABLE_LIVE_READS_ENV) is None
    assert os.environ.get("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE") is None
    records = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(records) == 1
    assert records[0]["agent_name"] == "airtable_context_agent"
    assert records[0]["dry_run"] == 0
    assert records[0]["model"] == "sdk-live:gpt-5.4-mini"


def test_marked_airtable_lifecycle_receives_process_local_operator_approval(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )

    def fake_run_typed_sdk_sync(_agent, _prompt, _output_type, **_kwargs):
        captured["approval"] = os.environ.get(
            "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
        )
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            cli.AirtableContextResult(
                mode="llm",
                summary="Prepared the bounded marked-record lifecycle.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Business Expenses"],
            ),
        )

    monkeypatch.delenv(
        "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE",
        raising=False,
    )
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert str(captured["approval"]).startswith("airtable-direct:")
    assert os.environ.get("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE") is None


def test_marked_zotero_lifecycle_receives_process_local_operator_approval(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    request = (
        "Create one marked standalone Zotero test note, verify it, revise the same "
        "note to be clearer, verify it again, and remove only that test note."
    )
    manual_plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    def fake_run_typed_sdk_sync(_agent, prompt, _output_type, **_kwargs):
        captured["approval"] = os.environ.get(
            "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE"
        )
        captured["prompt"] = prompt
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(summary="Prepared the bounded marked-note lifecycle."),
        )

    monkeypatch.delenv(
        "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE",
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "read_zotero_api_key_capabilities",
        lambda: {
            "status": "success",
            "provider_read": True,
            "operation": "verify_api_key_capabilities",
            "user_id_present": True,
            "user_library": True,
            "user_files": True,
            "user_notes": True,
            "user_write": True,
            "send_enabled": False,
        },
    )
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
        manual_plan=manual_plan,
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert str(captured["approval"]).startswith("zotero-direct:")
    assert str(captured["prompt"]).startswith(request)
    assert "Call the matching typed tool with live=true" in str(captured["prompt"])
    assert os.environ.get("KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE") is None


def test_direct_context_agent_receives_bounded_thread_identity_for_followup(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, str] = {}
    request = "Add this link to the notes: https://example.test/thread/123"
    execution_context = {
        "schema": "keystone.direct_specialist_context.v1",
        "prior_agent_runs": [
            {
                "route": "zotero_context_agent",
                "object_id": "ITEM123",
                "title": "Example article",
            }
        ],
    }

    def fake_run_typed_sdk_sync(_agent, prompt, _output_type, **_kwargs):
        captured["prompt"] = prompt
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(summary="Prepared the scoped note update."),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
        execution_context=execution_context,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["input"] == request
    assert captured["prompt"].startswith(request)
    assert "ITEM123" in captured["prompt"]
    assert "current operator request is authoritative" in captured["prompt"]


def test_direct_airtable_receipt_live_prompt_includes_selected_attachment(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    receipt = tmp_path / "Receipt-example.png"
    receipt.write_bytes(b"\x89PNG\r\n\x1a\nbounded test receipt")
    request = "add this attached receipt as exactly one personal expense in Airtable"
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")

    def fake_run_typed_sdk_sync(agent, prompt, _output_type, **_kwargs):
        captured["prompt"] = prompt
        captured["tool_names"] = {
            str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
            for tool in agent.tools or []
        }
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            cli.AirtableContextResult(
                mode="llm",
                summary="Prepared one receipt-backed personal expense create.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Personal Expenses"],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
        execution_context={
            "schema": "keystone.direct_specialist_context.v1",
            "thread_transcript_tail": f"Slack attachment materialized locally: {receipt}",
        },
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    prompt = captured["prompt"]
    assert isinstance(prompt, list)
    content = prompt[0]["content"]
    assert any(
        part.get("type") == "input_text"
        and str(receipt) in str(part.get("text") or "")
        for part in content
    )
    assert any(
        part.get("type") == "input_image"
        and str(part.get("image_url") or "").startswith("data:image/png;base64,")
        for part in content
    )
    assert "airtable_target_table" in str(prompt)
    assert captured["tool_names"] == {"airtable_create_expense_from_receipt"}


def test_direct_airtable_followup_update_receives_thread_record_identity(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    request = (
        "In Airtable, update the Description field on this personal expense to "
        "'Linear Basic - direct-call verification 2026-07-13', and verify the same record."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")

    def fake_run_typed_sdk_sync(agent, prompt, _output_type, **kwargs):
        captured["prompt"] = prompt
        captured["max_turns"] = kwargs.get("max_turns")
        captured["operator_approval"] = os.environ.get(
            "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
        )
        captured["tool_names"] = {
            str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
            for tool in agent.tools or []
        }
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            cli.AirtableContextResult(
                mode="llm",
                summary="Prepared the exact Personal Expenses update.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Personal Expenses"],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
        execution_context={
            "schema": "keystone.direct_specialist_context.v1",
            "thread_transcript_tail": (
                "Prior verified Airtable result: table Personal Expenses; "
                "record ID recSyntheticReceipt; record and attachment read-back passed."
            ),
        },
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert plan.intent == "business_system_write"
    assert "recSyntheticReceipt" in str(captured["prompt"])
    assert "Call the matching typed tool with live=true" in str(captured["prompt"])
    assert str(captured["operator_approval"]).startswith("airtable-direct:")
    assert captured["max_turns"] == 3
    assert "airtable_write_record" in captured["tool_names"]
    assert "airtable_get_base_schema" in captured["tool_names"]


def test_direct_specialist_context_is_selective_bounded_and_redacted() -> None:
    workflow_state = {
        "slack_context": {"channel_id": "C123", "thread_ts": "1783965218.778179"},
        "recent_slack_thread": [
            {
                "id": "1",
                "source_agent": "zotero_context_agent",
                "summary": "Resolved Example article as ITEM123 with api_key=secret-value.",
            }
        ],
        "prior_agent_runs": [
            {
                "route": "zotero_context_agent",
                "status": "success",
                "object_id": "ITEM123",
                "title": "Example article",
            }
        ],
        "slack_thread_transcript": "Resolved Example article as ITEM123.",
    }

    assert (
        cli._direct_specialist_execution_context(
            "Read the most recently added Zotero abstract.",
            workflow_state=workflow_state,
        )
        == {}
    )

    context = cli._direct_specialist_execution_context(
        "Add this link to the notes.",
        workflow_state=workflow_state,
    )
    serialized = json.dumps(context, sort_keys=True)
    assert context["schema"] == "keystone.direct_specialist_context.v1"
    assert "ITEM123" in serialized
    assert "secret-value" not in serialized
    assert len(serialized) < 5000

    forced_context = cli._direct_specialist_execution_context(
        "The path is /private/tmp/receipt.pdf.",
        workflow_state=workflow_state,
        force_thread_context=True,
    )
    assert forced_context["slack_scope"]["thread_ts"] == "1783965218.778179"
    assert "ITEM123" in json.dumps(forced_context, sort_keys=True)


def test_workitem_preflight_context_expands_exact_current_target_identity() -> None:
    work_item = WorkItem(
        id="wi_exact_gmail_draft",
        kind=WorkItemKind.GMAIL_THREAD,
        title="Revise the selected Gmail draft",
        request_text="Create the marked draft without sending it.",
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(
            name="KBA_TEST_DRAFT_EXACT",
            object_type="gmail_draft",
            external_id="draftExact123",
        ),
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="gmail_draft",
                artifact_id="draftExact123",
                source_agent="gmail_triage",
                title="KBA_TEST_DRAFT_EXACT",
                summary="Provider read-back verified the exact marked draft.",
                selected=True,
            )
        ],
        next_action=WorkItemNextAction(
            action="revise_draft",
            agent=WorkItemRoute.GMAIL_TRIAGE,
            description="Revise only the selected draft and verify the same ID.",
        ),
    )

    state = cli._orchestrator_workflow_state_from_cli_context(work_item=work_item)

    assert state["current_work_item"]["target"] == {
        "name": "KBA_TEST_DRAFT_EXACT",
        "object_type": "gmail_draft",
        "external_id": "draftExact123",
    }
    assert state["current_work_item"]["selected_artifacts"][0]["artifact_id"] == (
        "draftExact123"
    )
    assert state["current_work_item"]["next_action"]["action"] == "revise_draft"
    assert "metadata" not in state["current_work_item"]


def test_slack_context_file_does_not_replace_current_workitem_identity(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "selected-slack-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "channel_name": "ai-agents-workflow",
                "selected_message_ts": "1715366400.000100",
                "thread_ts": "1715366400.000100",
                "selected_message": {
                    "ts": "1715366400.000100",
                    "user_id": "U123",
                    "text": "Revise the same draft.",
                },
                "thread_messages": [
                    {
                        "ts": "1715366400.000100",
                        "user_id": "U123",
                        "text": "Revise the same draft.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    work_item = WorkItem(
        id="wi_exact_gmail_draft",
        kind=WorkItemKind.GMAIL_THREAD,
        title="Revise exact draft",
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(
            name="KBA_TEST_DRAFT_EXACT",
            object_type="gmail_draft",
            external_id="draftExact123",
        ),
    )

    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path),
        request_text="Make it shorter, and do not send.",
        work_item=work_item,
    )

    assert state["slack_context"]["channel_id"] == "C123"
    assert state["recent_slack_thread"][0]["summary"] == "Revise the same draft."
    assert state["current_work_item"]["target"]["external_id"] == "draftExact123"


def test_slack_history_context_preserves_root_and_speaker_roles_for_direct_and_graph_paths(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "slack-history-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C123",
                "thread_ts": "1770000000.000100",
                "request_ts": "1770000000.000400",
                "thread_root_request": (
                    "CoS, using only these facts, return exactly three bullets."
                ),
                "thread_messages": [
                    {
                        "ts": "1770000000.000100",
                        "role": "operator",
                        "source_agent": "UUSER",
                        "text": "CoS, using only these facts, return exactly three bullets.",
                    },
                    {
                        "ts": "1770000000.000200",
                        "role": "agent",
                        "source_agent": "kni",
                        "text": "Business Research Analyst article summary from a stale route.",
                    },
                ],
                "read_context": "Slack thread history digest with bounded history.",
            }
        ),
        encoding="utf-8",
    )

    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path),
        request_text="CoS, fix the reply using my original request.",
    )
    direct_context = cli._direct_specialist_execution_context(
        "Fix the same reply.",
        workflow_state=state,
        force_thread_context=True,
    )
    preflight = cli.run_orchestrator_preflight(
        "CoS, fix the reply using my original request.",
        requested_agent="chief_of_staff",
        workflow_state=state,
    )

    assert state["slack_thread_root"].startswith("CoS, using only these facts")
    assert state["recent_slack_thread"][0]["role"] == "operator"
    assert state["recent_slack_thread"][1]["role"] == "agent"
    assert direct_context["thread_root_request"] == state["slack_thread_root"]
    assert direct_context["recent_thread_messages"][1]["role"] == "agent"
    assert cli._bounded_direct_route_from_preflight(preflight) == "chief_of_staff"
    assert cli._preflight_requires_work_item(preflight) is False


def test_resumable_supplied_context_requires_work_item_even_if_planner_selects_one_owner() -> None:
    request = (
        "CoS, track this as a resumable internal review. Use only these approved "
        "facts: Northstar Care sells referral-navigation software; it has not supplied "
        "audited outcomes or an evaluation design. Assess what is supported and "
        "prepare a paste-ready internal Slack recommendation. Preserve the assessment "
        "and recommendation together so I can revise it later. Do not search or use "
        "provider tools."
    )
    preflight = cli.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
    )
    one_owner_plan = preflight.manual_request_plan.model_copy(
        update={
            "target_agent": "chief_of_staff",
            "workflow": [],
        }
    )
    one_owner_preflight = preflight.model_copy(
        update={
            "request_text": request,
            "manual_request_plan": one_owner_plan,
            "route_result": preflight.route_result.model_copy(
                update={
                    "route": "chief_of_staff",
                    "workflow": ["chief_of_staff"],
                }
            ),
        }
    )

    assert cli._preflight_requires_work_item(one_owner_preflight) is True


def test_mixed_slack_thread_correction_stays_direct_chief_instead_of_workitem(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context_path = tmp_path / "mixed-slack-history.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C123",
                "thread_ts": "1770000000.000100",
                "request_ts": "1770000000.000400",
                "thread_root_request": (
                    "CoS, using only these facts, give me exactly three bullets: "
                    "one shared envelope; separate backends; verified receipts."
                ),
                "thread_messages": [
                    {
                        "ts": "1770000000.000100",
                        "role": "operator",
                        "source_agent": "UUSER",
                        "text": (
                            "CoS, using only these facts, give me exactly three bullets."
                        ),
                    },
                    {
                        "ts": "1770000000.000200",
                        "role": "agent",
                        "source_agent": "kni",
                        "text": (
                            "Business Research Analyst article summary from a stale route."
                        ),
                    },
                ],
                "read_context": "Slack thread history digest with the same messages.",
            }
        ),
        encoding="utf-8",
    )
    envelope = "\n".join(
        [
            "business agents continue this prior Slack thread.",
            "Previous request: CoS give me the same three bullets. *Sent using*",
            "Previous result: Business Research Analyst article summary.",
            (
                "User follow-up: CoS, fix the last reply and give me only the same "
                "three bullets. *Sent using*"
            ),
            (
                "Continue the same agent task, treating the current user request as "
                "authoritative."
            ),
        ]
    )
    captured: dict[str, object] = {}

    def fake_run_specialist(route, input_text, **kwargs):
        captured["route"] = route
        captured["input_text"] = input_text
        captured["execution_context"] = kwargs.get("execution_context")
        return 0

    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_run_specialist)
    database_url = f"sqlite:///{tmp_path / 'mixed-thread.db'}"

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--no-live-manual-plan",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_path),
            envelope,
        ]
    )

    assert exit_code == 0
    assert captured["route"] == "chief_of_staff"
    assert captured["input_text"] == (
        "CoS give me the same three bullets.\n"
        "Prior result for context: Business Research Analyst article summary.\n"
        "Authoritative follow-up: fix the last reply and give me only the same "
        "three bullets."
    )
    execution_context = captured["execution_context"]
    assert isinstance(execution_context, dict)
    assert execution_context["thread_root_request"].startswith(
        "CoS, using only these facts"
    )
    assert SQLiteStore(database_url).list_work_items() == []


def test_slack_context_agents_keep_thread_scoped_sdk_continuity(tmp_path: Path) -> None:
    context_path = tmp_path / "slack-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "team_id": "T123",
                "channel_id": "C123",
                "thread_ts": "1783967611.595449",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        sdk_session=None,
        sdk_session_id="",
        sdk_session_db="",
        sdk_session_history_limit=None,
    )

    slack_spec = cli._sdk_session_spec_for_ask(
        args,
        route="zotero_context_agent",
        default_enabled=cli._ask_route_session_default("zotero_context_agent"),
        context_file_path=str(context_path),
    )
    cli_spec = cli._sdk_session_spec_for_ask(
        args,
        route="zotero_context_agent",
        default_enabled=cli._ask_route_session_default("zotero_context_agent"),
    )

    assert slack_spec.enabled is True
    assert slack_spec.scope == "slack"
    assert slack_spec.history_limit == 6
    assert cli_spec.enabled is False


def test_direct_gmail_followup_uses_bounded_thread_context_without_graph(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    execution_context = {
        "schema": "keystone.direct_specialist_context.v1",
        "recent_thread_messages": [
            {
                "source_agent": "operator",
                "summary": "From: Taylor. Subject: Pilot review. Can Tuesday work?",
            }
        ],
    }

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"output": {"summary": "Drafted a reply from selected context."}}
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_gmail_triage_live(
        "Draft a reply to this email confirming Tuesday works. Do not send.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        execution_context=execution_context,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output"]["summary"] == "Drafted a reply from selected context."
    command = captured["command"]
    assert isinstance(command, list)
    assert "scripts/run_gmail_triage.py" in command
    assert "--fixture" in command
    assert "--no-live-gmail" in command
    env = captured["env"]
    assert isinstance(env, dict)
    assert "KEYSTONE_SPECIALIST_EXECUTION_CONTEXT_JSON" in env


def test_direct_gmail_update_resolves_subject_only_from_matching_prior_route() -> None:
    context = {
        "prior_agent_runs": [
            {"route": "zotero_context_agent", "title": "Wrong source title"},
            {"route": "gmail_triage", "title": "Pilot review draft"},
        ]
    }

    assert (
        cli._direct_context_object_title(context, preferred_route="gmail_triage")
        == "Pilot review draft"
    )
    assert cli._direct_context_object_title(context, preferred_route="chief_of_staff") == ""


def test_zotero_latest_abstract_preacquires_provider_evidence_before_synthesis(
    monkeypatch,
    capsys,
) -> None:
    request = (
        "Use Zotero to select the most recently added journal article with a stored "
        "abstract. Provide its exact title and summarize the abstract."
    )

    captured: dict[str, str] = {}

    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_abstract_metadata",
        lambda: {
            "status": "success",
            "provider_read": True,
            "provider_order": {
                "sort": "dateAdded",
                "direction": "desc",
                "top_level_only": True,
                "item_type": "journalArticle",
            },
            "selection_rule": "first_nonempty_abstract_in_provider_order",
            "require_abstract": True,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "selected_item_has_abstract": True,
            "selected_item_date_added": "2026-07-12T12:00:00Z",
            "items": [
                {
                    "key": "ITEM123",
                    "data": {
                        "title": "Provider article",
                        "dateAdded": "2026-07-12T12:00:00Z",
                        "creators": [
                            {"firstName": "Amina", "lastName": "Researcher"},
                            {"name": "Clinical AI Consortium"},
                        ],
                        "publicationTitle": "Journal of Clinical AI",
                        "journalAbbreviation": "J Clin AI",
                        "date": "2026",
                        "abstractNote": "Provider abstract evidence.",
                    },
                }
            ],
        },
    )

    def fake_run_typed_sdk_sync(_agent, prompt, _output_type, **_kwargs):
        captured["tool_count"] = str(len(_agent.tools or []))
        captured["prompt"] = prompt
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(
                summary="Provider abstract evidence.",
                article_titles=["Provider article"],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"
    assert payload["block_kind"] == ""
    assert payload["side_effects"]["evidence_complete"] is True
    assert "Provider abstract evidence" in captured["prompt"]
    assert "Amina Researcher" in captured["prompt"]
    assert "Clinical AI Consortium" in captured["prompt"]
    assert "Journal of Clinical AI" in captured["prompt"]
    assert captured["tool_count"] == "0"
    assert payload["tool_receipts"][0]["provider_read"] is True


def test_zotero_latest_journal_metadata_preacquires_requested_fields(
    monkeypatch,
    capsys,
) -> None:
    request = (
        "Use Zotero to select the most recently added journal article. Return only "
        "its exact title, authors, and publication title. Do not use web search, "
        "full text, or modify Zotero."
    )
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_metadata",
        lambda **_kwargs: {
            "status": "success",
            "provider_read": True,
            "provider_order": {
                "sort": "dateAdded",
                "direction": "desc",
                "top_level_only": True,
                "item_type": "journalArticle",
            },
            "selection_rule": "provider_order",
            "require_abstract": False,
            "item_count": 1,
            "selected_item_title": "Newest provider article",
            "selected_item_date_added": "2026-07-13T12:00:00Z",
            "items": [
                {
                    "key": "ITEM456",
                    "data": {
                        "title": "Newest provider article",
                        "dateAdded": "2026-07-13T12:00:00Z",
                        "creators": [
                            {"firstName": "Amina", "lastName": "Researcher"},
                            {"name": "Clinical AI Consortium"},
                        ],
                        "publicationTitle": "Journal of Clinical AI",
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_abstract_metadata",
        lambda: (_ for _ in ()).throw(AssertionError("abstract-only read must not run")),
    )

    def fake_run_typed_sdk_sync(agent, prompt, _output_type, **_kwargs):
        captured["tool_count"] = str(len(agent.tools or []))
        captured["prompt"] = prompt
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(
                summary=(
                    "Title: Newest provider article\n"
                    "Authors: Amina Researcher; Clinical AI Consortium\n"
                    "Publication: Journal of Clinical AI"
                ),
                article_titles=["Newest provider article"],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"
    assert payload["block_kind"] == ""
    assert payload["side_effects"]["evidence_complete"] is True
    assert "Amina Researcher" in captured["prompt"]
    assert "Clinical AI Consortium" in captured["prompt"]
    assert "Journal of Clinical AI" in captured["prompt"]
    assert captured["tool_count"] == "0"
    assert payload["tool_receipts"][0]["provider_read"] is True
    assert payload["tool_receipts"][0]["require_abstract"] is False


def test_zotero_latest_article_preflight_preserves_notes_and_attachment_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_metadata",
        lambda **_kwargs: {
            "status": "success",
            "provider_read": True,
            "provider_order": {"sort": "dateAdded", "direction": "desc"},
            "selection_rule": "provider_order",
            "require_abstract": False,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "items": [
                {
                    "key": "PARENT1",
                    "data": {"title": "Provider article", "itemType": "journalArticle"},
                }
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_item_children",
        lambda **_kwargs: json.dumps(
            {
                "status": "success",
                "provider_read": True,
                "parent_item_key": "PARENT1",
                "note_count": 1,
                "attachment_count": 1,
                "children": [
                    {
                        "item_key": "NOTE1",
                        "item_type": "note",
                        "parent_item_key": "PARENT1",
                        "note": "<p>Stored note evidence.</p>",
                    },
                    {
                        "item_key": "PDF1",
                        "item_type": "attachment",
                        "parent_item_key": "PARENT1",
                        "filename": "article.pdf",
                        "content_type": "application/pdf",
                    },
                ],
            }
        ),
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Use Zotero to select the most recently added article and list its notes "
        "and available attachments.",
    )

    assert blocker == ""
    assert "Stored note evidence" in context
    assert "PDF1" in context
    assert receipts[1] == {
        "status": "success",
        "provider_read": True,
        "operation": "read_item_children",
        "note_count": 1,
        "attachment_count": 1,
    }


def test_zotero_latest_article_preflight_reads_exact_pdf_only_on_demand(
    monkeypatch,
) -> None:
    pdf_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_metadata",
        lambda **_kwargs: {
            "status": "success",
            "provider_read": True,
            "provider_order": {"sort": "dateAdded", "direction": "desc"},
            "selection_rule": "provider_order",
            "require_abstract": False,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "items": [
                {
                    "key": "PARENT1",
                    "data": {"title": "Provider article", "itemType": "journalArticle"},
                }
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_item_children",
        lambda **_kwargs: json.dumps(
            {
                "status": "success",
                "provider_read": True,
                "note_count": 0,
                "attachment_count": 1,
                "children": [
                    {
                        "item_key": "PDF1",
                        "item_type": "attachment",
                        "parent_item_key": "PARENT1",
                        "filename": "article.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ),
    )

    def fake_pdf_read(**kwargs: object) -> str:
        pdf_calls.append(kwargs)
        return json.dumps(
            {
                "status": "success",
                "provider_read": True,
                "parent_item_key": "PARENT1",
                "attachment_item_key": "PDF1",
                "page_count": 2,
                "pages_read": 2,
                "char_count": 27,
                "text": "Exact attached PDF evidence.",
                "truncated": False,
                "file_persisted": False,
            }
        )

    monkeypatch.setattr(cli, "zotero_read_pdf_attachment_text", fake_pdf_read)

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Use Zotero to select the most recently added article, read its attached PDF, "
        "and summarize the paper.",
    )

    assert blocker == ""
    assert "Exact attached PDF evidence" in context
    assert pdf_calls == [
        {
            "parent_item_key": "PARENT1",
            "attachment_item_key": "PDF1",
            "max_pages": 25,
            "max_chars": 50000,
            "live": True,
        }
    ]
    assert receipts[-1]["operation"] == "read_pdf_attachment_text"


def test_live_semantic_zotero_read_plan_ignores_incidental_write_words(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_zotero_api_key_capabilities",
        lambda: (_ for _ in ()).throw(AssertionError("write preflight must not run")),
    )
    plan = ManualRequestPlan(
        source="llm",
        target_agent="zotero_context_agent",
        provider_system="zotero",
        intent="context_lookup",
        target_type="zotero_article",
        provider_operations=["read"],
        objective="Read the selected Zotero article.",
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "The prior note was created and updated; now explain the selected article.",
        manual_plan=plan,
    )

    assert (context, receipts, blocker) == ("", [], "")


def test_zotero_write_preflight_blocks_rejected_key_before_specialist(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_zotero_api_key_capabilities",
        lambda: (_ for _ in ()).throw(PermissionError("provider rejected key")),
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Make a temporary Zotero note and then remove it.",
    )

    assert context == ""
    assert receipts == [
        {
            "status": "error",
            "provider_read": False,
            "operation": "verify_api_key_capabilities",
            "error_type": "PermissionError",
        }
    ]
    assert "/keys/current" in blocker
    assert "no Zotero data was modified" in blocker


def test_zotero_write_preflight_requires_user_library_write_access(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_zotero_api_key_capabilities",
        lambda: {
            "status": "success",
            "provider_read": True,
            "operation": "verify_api_key_capabilities",
            "user_id_present": True,
            "user_library": True,
            "user_files": True,
            "user_notes": True,
            "user_write": False,
            "send_enabled": False,
        },
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Revise this Zotero item note.",
    )

    assert context == ""
    assert receipts[0]["user_write"] is False
    assert "does not grant user-library write access" in blocker


def test_zotero_latest_article_preflight_respects_no_full_text_constraint(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_metadata",
        lambda **_kwargs: {
            "status": "success",
            "provider_read": True,
            "provider_order": {"sort": "dateAdded", "direction": "desc"},
            "selection_rule": "provider_order",
            "require_abstract": False,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "items": [
                {
                    "key": "PARENT1",
                    "data": {"title": "Provider article", "itemType": "journalArticle"},
                }
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_item_children",
        lambda **_kwargs: json.dumps(
            {
                "status": "success",
                "provider_read": True,
                "note_count": 0,
                "attachment_count": 1,
                "children": [
                    {
                        "item_key": "PDF1",
                        "item_type": "attachment",
                        "filename": "article.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_pdf_attachment_text",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("PDF text must not be read when expressly forbidden")
        ),
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Use Zotero to select the most recently added article and list its PDF "
        "attachment, but do not use full text or read the PDF.",
    )

    assert blocker == ""
    assert "article.pdf" in context
    assert all(receipt.get("operation") != "read_pdf_attachment_text" for receipt in receipts)


def test_zotero_latest_article_preflight_does_not_guess_between_multiple_pdfs(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_metadata",
        lambda **_kwargs: {
            "status": "success",
            "provider_read": True,
            "provider_order": {"sort": "dateAdded", "direction": "desc"},
            "selection_rule": "provider_order",
            "require_abstract": False,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "items": [
                {
                    "key": "PARENT1",
                    "data": {"title": "Provider article", "itemType": "journalArticle"},
                }
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_item_children",
        lambda **_kwargs: json.dumps(
            {
                "status": "success",
                "provider_read": True,
                "note_count": 0,
                "attachment_count": 2,
                "children": [
                    {
                        "item_key": key,
                        "item_type": "attachment",
                        "filename": filename,
                        "content_type": "application/pdf",
                    }
                    for key, filename in (("PDF1", "main.pdf"), ("PDF2", "supplement.pdf"))
                ],
            }
        ),
    )
    monkeypatch.setattr(
        cli,
        "zotero_read_pdf_attachment_text",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an ambiguous PDF attachment must not be selected")
        ),
    )

    context, receipts, blocker = cli._direct_zotero_provider_preflight(
        "zotero_context_agent",
        "Use Zotero to select the most recently added article, read its attached PDF, "
        "and summarize the paper.",
    )

    assert blocker == ""
    assert '"status": "ambiguous"' in context
    assert "name one attachment" in context
    assert all(receipt.get("operation") != "read_pdf_attachment_text" for receipt in receipts)


def test_context_agent_public_blockers_deduplicate_mapping_messages() -> None:
    blockers = cli._context_agent_public_blockers(
        {
            "blockers": [
                "Provider field is unavailable.",
                "Provider field is unavailable.",
            ]
        },
        execution_blocker="Output validation needs clarification.",
    )

    assert blockers == [
        {"message": "Provider field is unavailable."},
        {"message": "Output validation needs clarification."},
    ]


def test_zotero_thread_followup_keeps_read_tools_for_new_metadata(
    monkeypatch,
    capsys,
) -> None:
    request = (
        "For the same Zotero article, who are the authors, where was it published, "
        "and summarize the stored abstract in no more than 100 words."
    )
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_sync(agent, prompt, _output_type, **kwargs):
        captured["tool_names"] = {
            str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
            for tool in agent.tools or []
        }
        captured["prompt"] = prompt
        captured["session"] = kwargs.get("session")
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(
                summary="The stored abstract reports a bounded follow-up synthesis.",
                article_titles=["Provider article"],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
        execution_context={
            "schema": "keystone.direct_specialist_context.v1",
            "prior_agent_runs": [
                {
                    "route": "zotero_context_agent",
                    "title": "Provider article",
                    "summary": "Prior abstract summary.",
                }
            ],
        },
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert "zotero_read_api_metadata" in captured["tool_names"]
    assert "Provider article" in str(captured["prompt"])


def test_direct_context_agent_tool_tier_separates_reads_from_writes() -> None:
    read_plan = ManualRequestPlan(
        target_agent="airtable_context_agent",
        objective="Read the Airtable schema.",
        intent="context_lookup",
    )
    write_plan = ManualRequestPlan(
        target_agent="airtable_context_agent",
        objective="Update one exact Airtable record.",
        intent="business_system_write",
    )

    assert cli._direct_context_agent_tool_tier(read_plan) == "core_read"
    assert cli._direct_context_agent_tool_tier(write_plan) == "internal_write"
    assert cli._direct_context_agent_tool_tier(None) == "core_read"
    assert (
        cli._direct_context_agent_tool_tier(
                None,
                input_text=(
                    "Using Airtable context, create one marked KBA test expense in the "
                    "Business Expenses table, verify it, update the same record "
                    "description, verify it again, and remove only that test record."
                ),
        )
        == "internal_write"
    )

    read_agent = cli.AGENT_REGISTRY["airtable_context_agent"].build_agent(
        request_text=read_plan.objective,
        tool_tier=cli._direct_context_agent_tool_tier(read_plan),
    )
    write_agent = cli.AGENT_REGISTRY["airtable_context_agent"].build_agent(
        request_text=write_plan.objective,
        tool_tier=cli._direct_context_agent_tool_tier(write_plan),
    )
    read_names = {
        str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        for tool in read_agent.tools or []
    }
    write_names = {
        str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        for tool in write_agent.tools or []
    }

    assert read_names == {"airtable_get_base_schema", "airtable_read_records"}
    assert "airtable_write_record" in write_names
    assert read_names < write_names


def test_live_semantic_read_plan_cannot_gain_write_tools_from_request_words() -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent="airtable_context_agent",
        provider_system="airtable",
        objective="Read the exact marked Airtable record.",
        intent="context_lookup",
        provider_operations=["read", "verify"],
    )
    noisy_request = (
        "The prior test created and updated KBA_TEST_RECORD_001; now read and verify it."
    )

    assert (
        cli._direct_context_agent_tool_tier(plan, input_text=noisy_request)
        == "core_read"
    )


def test_zotero_strict_abstract_answer_promotes_substantive_model_evidence(
    monkeypatch,
    capsys,
) -> None:
    request = (
        "Use Zotero to select the most recently added journal article with a stored "
        "abstract. Provide its exact title and summarize the abstract in no more than "
        "50 words."
    )
    monkeypatch.setattr(
        cli,
        "read_latest_zotero_journal_abstract_metadata",
        lambda: {
            "status": "success",
            "provider_read": True,
            "provider_order": {
                "sort": "dateAdded",
                "direction": "desc",
                "top_level_only": True,
                "item_type": "journalArticle",
            },
            "selection_rule": "first_nonempty_abstract_in_provider_order",
            "require_abstract": True,
            "item_count": 1,
            "selected_item_title": "Provider article",
            "selected_item_has_abstract": True,
            "selected_item_date_added": "2026-07-12T12:00:00Z",
            "items": [
                {
                    "key": "ITEM123",
                    "data": {
                        "title": "Provider article",
                        "abstractNote": "A scoping review evaluated relapse detection.",
                    },
                }
            ],
        },
    )

    def fake_run_typed_sdk_sync(_agent, _prompt, _output_type, **_kwargs):
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(
                summary=(
                    "Selected the most recently added journal article and summarized "
                    "its abstract in 50 words or fewer."
                ),
                article_titles=["Provider article"],
                relevant_evidence=[
                    "Provider metadata confirms the ordered item.",
                    (
                        "The stored abstract reports a scoping review of AI relapse "
                        "detection using smartphones and wearables, with heterogeneous "
                        "performance and limited replication."
                    ),
                ],
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)
    plan = ManualRequestPlan(
        requested_agent="business_research_analyst",
        target_agent="zotero_context_agent",
        objective=request,
        intent="context_lookup",
        expected_artifact_type="context_summary",
        ask_shape={
            "output_form": "brief",
            "strict_filter_mode": "exact",
            "stop_condition": "stop_after_50_word_summary",
        },
    )

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output"]["summary"].startswith("The stored abstract reports")
    assert payload["human_summary"].startswith("Title: Provider article\nSummary: The stored")
    assert "Selected the most recently" not in payload["human_summary"]


def test_context_agent_tool_receipts_are_bounded_and_report_verified_writes() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "update",
                        "table": "Business Expenses",
                        "record_id": "recSynthetic",
                        "approval_reference": "approval:test:update",
                        "record": {"private_field": "must not enter receipt"},
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "record_id_match": True,
                            "matched_fields": ["Description"],
                            "mismatched_fields": [],
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "update",
            "table": "Business Expenses",
            "record_id": "recSynthetic",
            "approval_reference": "approval:test:update",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "record_id_match": True,
                "matched_fields": ["Description"],
                "mismatched_fields": [],
            },
        }
    ]


def test_context_agent_tool_receipts_report_verified_receipt_create_and_attachment() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "create_expense_from_receipt",
                        "table": "Personal Expenses",
                        "record_id": "recSyntheticReceipt",
                        "approval_reference": "airtable-direct:test",
                        "write_result": {"record": {"private": "omitted"}},
                        "attachment_result": {"attachment": {"private": "omitted"}},
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "record_id_match": True,
                            "create_read_back": True,
                            "attachment_read_back": True,
                            "attachment_count_before": 0,
                            "attachment_count_after": 1,
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "create_expense_from_receipt",
            "table": "Personal Expenses",
            "record_id": "recSyntheticReceipt",
            "approval_reference": "airtable-direct:test",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "record_id_match": True,
                "create_read_back": True,
                "attachment_read_back": True,
                "attachment_count_before": 0,
                "attachment_count_after": 1,
            },
        }
    ]
    assert cli._context_agent_external_write_performed(receipts) is True


def test_context_agent_tool_receipts_preserve_duplicate_cleanup_evidence() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "reconcile_duplicate_expense",
                        "table": "Personal Expenses",
                        "record_id": "recKeep123",
                        "duplicate_record_id": "recDuplicate456",
                        "target_estimated_tax_period": "3",
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "record_id_match": True,
                            "updated_period": True,
                            "attachment_read_back": True,
                            "attachment_filenames": ["receipt.pdf"],
                            "duplicate_provider_deleted": True,
                            "duplicate_record_absent_after": True,
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    assert cli._context_agent_tool_receipts(raw_result) == [
        {
            "status": "success",
            "operation": "reconcile_duplicate_expense",
            "table": "Personal Expenses",
            "record_id": "recKeep123",
            "duplicate_record_id": "recDuplicate456",
            "target_estimated_tax_period": "3",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "record_id_match": True,
                "attachment_read_back": True,
                "updated_period": True,
                "attachment_filenames": ["receipt.pdf"],
                "duplicate_provider_deleted": True,
                "duplicate_record_absent_after": True,
            },
        }
    ]


def test_airtable_receipt_execution_blocker_requires_both_provider_read_backs() -> None:
    request = (
        "Add this attached receipt as exactly one personal expense in Airtable, "
        "attach the PDF, and verify the created record and attachment."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")
    successful_receipt = {
        "status": "success",
        "operation": "create_expense_from_receipt",
        "record_id": "recSyntheticReceipt",
        "verification": {
            "passed": True,
            "create_read_back": True,
            "attachment_read_back": True,
        },
    }
    partial_receipt = {
        **successful_receipt,
        "status": "partial",
        "verification": {
            "passed": False,
            "create_read_back": True,
            "attachment_read_back": False,
        },
    }

    assert (
        cli._airtable_receipt_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[successful_receipt],
        )
        == ""
    )
    blocker = cli._airtable_receipt_execution_blocker(
        "airtable_context_agent",
        request,
        manual_plan=plan,
        tool_receipts=[partial_receipt],
    )
    assert "partially completed" in blocker
    assert "avoid a duplicate expense" in blocker
    assert cli._context_agent_external_write_performed([partial_receipt]) is True


def test_airtable_receipt_reconciliation_accepts_complete_provider_verification() -> None:
    request = (
        "Reconcile these duplicate Airtable receipt records. Keep recKeep123, change "
        "Estimated Tax Periods to 3, preserve receipt.pdf, and remove recDuplicate456."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")
    receipt = {
        "status": "success",
        "operation": "reconcile_duplicate_expense",
        "record_id": "recKeep123",
        "duplicate_record_id": "recDuplicate456",
        "target_estimated_tax_period": "3",
        "verification": {
            "passed": True,
            "record_id_match": True,
            "updated_period": True,
            "attachment_read_back": True,
            "duplicate_provider_deleted": True,
            "duplicate_record_absent_after": True,
        },
    }

    assert (
        cli._airtable_receipt_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[receipt],
        )
        == ""
    )
    assert cli._context_agent_external_write_performed([receipt]) is True


def test_airtable_receipt_update_accepts_verified_same_record_mutation() -> None:
    request = (
        "Update Airtable Personal Expenses record recKeep123 in place. Set Item to "
        "Linear Basic, preserve receipt.pdf, and forbid record creation."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")
    receipt = {
        "status": "success",
        "operation": "update",
        "record_id": "recKeep123",
        "verification": {
            "passed": True,
            "record_id_match": True,
            "matched_fields": ["Item"],
            "mismatched_fields": [],
        },
    }

    assert (
        cli._airtable_receipt_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[receipt],
        )
        == ""
    )


def test_airtable_generic_write_scope_binds_update_and_reconcile_without_create() -> None:
    update_request = (
        "In Airtable update record recKeep123 in place and verify it; do not create "
        "another record."
    )
    update_plan = ManualRequestPlan(
        requested_agent="airtable_context_agent",
        target_agent="airtable_context_agent",
        intent="business_system_write",
        target_type="business_system_context",
        objective="Update one exact Airtable record in place.",
    )
    reconcile_request = (
        "Reconcile the two exact duplicate Airtable records and keep recKeep123; "
        "do not create anything new."
    )

    assert (
        cli._direct_airtable_allowed_operation(
            update_plan,
            input_text=update_request,
        )
        == "update"
    )
    assert (
        cli._direct_airtable_allowed_operation(
            update_plan,
            input_text=reconcile_request,
        )
        == "update"
    )


@pytest.mark.parametrize(
    ("provider_operations", "noisy_request", "expected"),
    [
        (["update", "verify"], "Tighten the saved value; no create wording is required.", "update"),
        (["create", "verify"], "The note mentions an earlier update, but add one record now.", "create"),
        (["create", "update", "verify"], "Handle both stages in order.", ""),
    ],
)
def test_live_semantic_airtable_operations_override_raw_verb_noise(
    provider_operations: list[str],
    noisy_request: str,
    expected: str,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="airtable_context_agent",
        provider_system="airtable",
        intent="business_system_write",
        target_type="business_system_context",
        provider_operations=provider_operations,
        objective="Perform the normalized Airtable operation.",
    )

    assert (
        cli._direct_airtable_allowed_operation(plan, input_text=noisy_request)
        == expected
    )


def test_airtable_receipt_read_only_verification_does_not_require_write_receipt() -> None:
    request = (
        "Verify the Airtable Personal Expenses receipt cleanup only; do not modify "
        "anything. Confirm recKeep123 retains receipt.pdf and recDuplicate456 is absent."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")

    assert (
        cli._airtable_receipt_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[],
        )
        == ""
    )


def test_airtable_write_execution_blocker_requires_typed_verified_mutation() -> None:
    request = (
        "In Airtable, update the Description field on this personal expense to "
        "'Linear Basic - direct-call verification 2026-07-13', and verify the same record."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")
    verified_receipt = {
        "status": "success",
        "operation": "update",
        "record_id": "recSyntheticReceipt",
        "verification": {
            "passed": True,
            "record_id_match": True,
            "matched_fields": ["Description"],
            "mismatched_fields": [],
        },
    }

    assert (
        cli._airtable_write_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[verified_receipt],
        )
        == ""
    )
    missing_receipt_blocker = cli._airtable_write_execution_blocker(
        "airtable_context_agent",
        request,
        manual_plan=plan,
        tool_receipts=[],
    )
    assert "was not executed" in missing_receipt_blocker

    unverified_receipt = {
        **verified_receipt,
        "verification": {
            "passed": False,
            "record_id_match": True,
            "matched_fields": [],
            "mismatched_fields": ["Description"],
        },
    }
    unverified_blocker = cli._airtable_write_execution_blocker(
        "airtable_context_agent",
        request,
        manual_plan=plan,
        tool_receipts=[unverified_receipt],
    )
    assert "read-after-write verification did not pass" in unverified_blocker
    assert "Do not retry blindly" in unverified_blocker


def test_airtable_write_execution_blocker_accepts_verified_test_lifecycle() -> None:
    request = (
        "In Airtable Business Expenses, add KBA_TEST_RECORD_ANU120_R7, change the "
        "same record, then remove it and confirm absence."
    )
    plan = infer_manual_request_plan(request, requested_agent="airtable_context_agent")
    lifecycle_receipt = {
        "status": "success",
        "operation": "test_record_lifecycle",
        "record_id": "recSyntheticLifecycle",
        "verification": {
            "passed": True,
            "create_read_back": True,
            "same_record_update_read_back": True,
            "record_absent_after_cleanup": True,
        },
    }

    assert (
        cli._airtable_write_execution_blocker(
            "airtable_context_agent",
            request,
            manual_plan=plan,
            tool_receipts=[lifecycle_receipt],
        )
        == ""
    )


def test_context_agent_public_blockers_deduplicates_nested_blocker_objects() -> None:
    blockers = cli._context_agent_public_blockers(
        {
            "blockers": [
                "Provider evidence is incomplete.",
                {"message": "Provider evidence is incomplete."},
                {"message": "Exact record identity is required."},
            ]
        },
        execution_blocker="Exact record identity is required.",
    )

    assert blockers == [
        {"message": "Provider evidence is incomplete."},
        {"message": "Exact record identity is required."},
    ]


def test_zotero_ordered_abstract_receipt_proves_live_provider_selection() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "provider_read": True,
                        "provider_order": {
                            "sort": "dateAdded",
                            "direction": "desc",
                            "top_level_only": True,
                            "item_type": "journalArticle",
                        },
                        "selection_rule": "first_nonempty_abstract_in_provider_order",
                        "require_abstract": True,
                        "selected_item_title": "Synthetic article",
                        "selected_item_has_abstract": True,
                        "selected_item_date_added": "2026-07-12T12:00:00Z",
                        "items": [{"private_abstract": "must not enter receipt"}],
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "provider_read": True,
            "provider_order": {
                "sort": "dateAdded",
                "direction": "desc",
                "top_level_only": True,
                "item_type": "journalArticle",
            },
            "selection_rule": "first_nonempty_abstract_in_provider_order",
            "require_abstract": True,
            "selected_item_title": "Synthetic article",
            "selected_item_has_abstract": True,
            "selected_item_date_added": "2026-07-12T12:00:00Z",
            "send_enabled": False,
        }
    ]
    assert (
        cli._zotero_ordered_abstract_receipt_blocker(
            "zotero_context_agent",
            "Use Zotero to select the most recently added journal article with a "
            "stored abstract.",
            receipts,
        )
        == ""
    )
    assert "live, ordered provider metadata read" in (
        cli._zotero_ordered_abstract_receipt_blocker(
            "zotero_context_agent",
            "Use Zotero to select the most recently added journal article with a "
            "stored abstract.",
            [{"status": "success", "source": "local_zotero_cache"}],
        )
    )


def test_context_agent_blocked_read_is_not_reported_as_blocked_write() -> None:
    receipts = [
        {"status": "blocked", "table": "Business Expenses", "send_enabled": False},
        {
            "status": "success",
            "operation": "create",
            "table": "Business Expenses",
            "approval_reference": "approval:test:create",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        },
    ]

    assert cli._context_agent_blocked_write_attempts(
        {"agent_name": "airtable_context_agent"},
        tool_receipts=receipts,
    ) == []
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private_field" not in json.dumps(receipts)


def test_context_agent_lifecycle_receipt_keeps_bounded_verification() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "test_record_lifecycle",
                        "table": "Business Expenses",
                        "record_id": "recInternalOnly",
                        "approval_reference": "airtable-direct:requesthash",
                        "required_marker": "KBA_TEST_RECORD",
                        "verification": {
                            "passed": True,
                            "create_read_back": True,
                            "same_record_update_read_back": True,
                            "record_absent_after_cleanup": True,
                        },
                        "create": {"record": {"private": "discard"}},
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts[0]["operation"] == "test_record_lifecycle"
    assert receipts[0]["verification"] == {
        "passed": True,
        "create_read_back": True,
        "same_record_update_read_back": True,
        "record_absent_after_cleanup": True,
    }
    assert "create" not in receipts[0]
    assert cli._context_agent_external_write_performed(receipts) is True


def test_airtable_lifecycle_public_payload_removes_provider_identity() -> None:
    provider_id = "recProviderInternal123"
    output = {
        "base_id": "appProviderInternal123",
        "candidate_record_ids": [provider_id],
        "recommended_record_identity": f"Airtable record {provider_id} was removed.",
        "record_summaries": [
            {
                "key": provider_id,
                "value": "Lifecycle passed.",
                "note": f"Updated {provider_id} before cleanup.",
            }
        ],
        "executed_write_results": [
            {"key": "create", "value": "success", "note": f"Created {provider_id}."}
        ],
        "write_plan": {
            "field_mapping": [
                {"key": "required_marker", "value": "KBA_TEST_RECORD"},
                {"key": "record_id", "value": provider_id},
            ]
        },
    }
    receipts = [
        {
            "status": "success",
            "operation": "test_record_lifecycle",
            "record_id": provider_id,
            "verification": {"passed": True},
        }
    ]

    public_output, public_receipts = cli._context_agent_public_payload(
        "airtable_context_agent",
        output,
        receipts,
    )

    rendered = json.dumps({"output": public_output, "receipts": public_receipts})
    assert provider_id not in rendered
    assert "appProviderInternal123" not in rendered
    assert public_output["candidate_record_ids"] == []
    assert public_output["base_id"] == ""
    assert public_output["write_plan"]["field_mapping"] == [
        {"key": "required_marker", "value": "KBA_TEST_RECORD"}
    ]
    assert "record_id" not in public_receipts[0]


def test_zotero_lifecycle_public_payload_removes_provider_identity() -> None:
    provider_id = "NOTEINTERNAL"
    output = {
        "zotero_item_keys": [provider_id],
        "relevant_evidence": [f"Updated {provider_id} and verified it."],
        "executed_note_results": [
            {"key": provider_id, "value": "success", "note": "Cleanup passed."}
        ],
    }
    receipts = [
        {
            "status": "success",
            "operation": "test_note_lifecycle",
            "item_key": provider_id,
            "verification": {"passed": True},
        }
    ]

    public_output, public_receipts = cli._context_agent_public_payload(
        "zotero_context_agent",
        output,
        receipts,
    )

    rendered = json.dumps({"output": public_output, "receipts": public_receipts})
    assert provider_id not in rendered
    assert public_output["zotero_item_keys"] == []
    assert "the marked test note" in rendered
    assert "item_key" not in public_receipts[0]


def test_context_agent_tool_receipts_report_zotero_delete_and_absence() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "delete_test_note",
                        "item_key": "NOTEKBA1",
                        "approval_reference": "approval:test:delete",
                        "before": {"note_sha256": "private-content-hash"},
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "item_absent_after": True,
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "delete_test_note",
            "item_key": "NOTEKBA1",
            "approval_reference": "approval:test:delete",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "item_absent_after": True,
            },
        }
    ]
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private-content-hash" not in json.dumps(receipts)


def test_context_agent_tool_receipts_classify_workspace_sheet_lifecycle() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "create_sheet",
            "spreadsheet_id": "sheetKBA1",
            "title": "KBA_TEST_SHEET sdk-live",
            "folder_path": "KNIOps",
            "approval_reference": "approval:create",
            "verification": {
                "status": "verified",
                "passed": True,
                "spreadsheet_id_match": True,
                "title_match": True,
                "mime_type_match": True,
                "trashed": False,
                "trashed_match": True,
            },
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "append_rows",
            "spreadsheet_id": "sheetKBA1",
            "sheet_name": "Validation",
            "row_count": 1,
            "updated_range": "Validation!A2:C2",
            "approval_reference": "approval:append",
            "verification": {
                "status": "verified",
                "passed": True,
                "row_count_match": True,
                "values_match": True,
                "verified_row_count": 1,
            },
            "rows": [["private", "row", "content"]],
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "update_row",
            "spreadsheet_id": "sheetKBA1",
            "sheet_name": "Validation",
            "row_number": 2,
            "key_column": "record_key",
            "key_value": "KBA_TEST_ROW_1",
            "approval_reference": "approval:update",
            "verification": {
                "status": "verified",
                "passed": True,
                "row_found": True,
                "row_number": 2,
                "matched_fields": ["note", "status"],
                "mismatched_fields": [],
            },
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "trash_sheet",
            "spreadsheet_id": "sheetKBA1",
            "trashed": True,
            "approval_reference": "approval:trash",
            "verification": {
                "status": "verified",
                "passed": True,
                "spreadsheet_id_match": True,
                "title_match": True,
                "mime_type_match": True,
                "trashed": True,
                "trashed_match": True,
            },
            "send_enabled": False,
        },
    ]
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(output))
            for output in outputs
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert [receipt["operation"] for receipt in receipts] == [
        "create_sheet",
        "append_rows",
        "update_row",
        "trash_sheet",
    ]
    assert all(receipt["spreadsheet_id"] == "sheetKBA1" for receipt in receipts)
    assert receipts[1]["row_count"] == 1
    assert receipts[1]["verification"]["values_match"] is True
    assert receipts[2]["key_value"] == "KBA_TEST_ROW_1"
    assert receipts[2]["verification"]["matched_fields"] == ["note", "status"]
    assert receipts[3]["trashed"] is True
    assert receipts[3]["verification"]["trashed_match"] is True
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private" not in json.dumps(receipts)


def test_context_agent_verified_write_reconciles_direct_write_plan() -> None:
    output_payload: dict[str, object] = {
        "summary": "The update was not executed.",
        "blockers": ["The write was not executed because approval is still required."],
        "approval_needs": ["Live write approval is still required."],
        "write_plan": {
            "approval_required": True,
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
        }
    }
    receipts = [
        {
            "status": "success",
            "operation": "create_sheet",
            "verification": {"status": "verified", "passed": True},
        }
    ]

    cli._reconcile_context_agent_executed_write_plan(output_payload, receipts)

    write_plan = output_payload["write_plan"]
    assert isinstance(write_plan, dict)
    assert write_plan["approval_required"] is True
    assert write_plan["approval_reference_needed"] is False
    assert write_plan["live_write_allowed_for_specialist"] is True
    assert output_payload["blockers"] == []
    assert output_payload["approval_needs"] == []
    assert cli._verified_context_agent_write_summary(receipts) == (
        "Created and provider-verified the exact requested item."
    )


def test_verified_airtable_reconciliation_summary_uses_provider_receipt() -> None:
    receipts = [
        {
            "status": "success",
            "operation": "reconcile_duplicate_expense",
            "table": "Personal Expenses",
            "record_id": "recKeep123",
            "duplicate_record_id": "recDuplicate456",
            "verification": {
                "passed": True,
                "duplicate_record_absent_after": True,
            },
        }
    ]

    assert cli._verified_context_agent_write_summary(receipts) == (
        "Reconciled and provider-verified the exact Airtable expense record "
        "recKeep123 in place; duplicate recDuplicate456 was removed and verified "
        "absent."
    )


def test_context_agent_slide_copy_receipts_preserve_provenance_and_classify_write() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "extract_slide_copy",
            "slide_number": 2,
            "output_format": "png",
            "artifact_path": "artifacts/presentation-derived/KBA_TEST_SLIDE-2.png",
            "artifact_size": 1234,
            "artifact_sha256": "a" * 64,
            "parent_content_sha256": "b" * 64,
            "parent_modified": False,
            "derived_copy_created": True,
            "approval_reference": "approved:slide-copy",
            "verification": {
                "status": "verified",
                "passed": True,
                "artifact_exists": True,
                "file_signature_valid": True,
                "parent_hash_match": True,
                "parent_mtime_match": True,
            },
            "send_enabled": False,
        }
    ]

    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(outputs[0]))
        ]
    )
    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts[0]["slide_number"] == 2
    assert receipts[0]["artifact_path"].endswith("KBA_TEST_SLIDE-2.png")
    assert receipts[0]["verification"]["parent_hash_match"] is True
    assert cli._context_agent_external_write_performed(receipts) is True


def test_context_agent_unverified_write_does_not_relax_write_plan() -> None:
    output_payload: dict[str, object] = {
        "write_plan": {
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
        }
    }
    receipts = [
        {
            "status": "success",
            "operation": "create_sheet",
            "verification": {"status": "verification_failed", "passed": False},
        }
    ]

    cli._reconcile_context_agent_executed_write_plan(output_payload, receipts)

    write_plan = output_payload["write_plan"]
    assert isinstance(write_plan, dict)
    assert write_plan["approval_reference_needed"] is True
    assert write_plan["live_write_allowed_for_specialist"] is False


def test_context_agent_workspace_read_receipts_keep_identity_without_content() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "search_files",
            "query": "proposal",
            "mime_type": "application/vnd.google-apps.document",
            "folder_path": "KNIOps",
            "item_count": 1,
            "items": [{"id": "private-file-id", "name": "private file name"}],
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "read_doc",
            "document_id": "docKBA1",
            "title": "Selected internal document",
            "char_count": 412,
            "truncated": False,
            "text": "private document body must not enter the receipt",
            "send_enabled": False,
        },
    ]
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(output))
            for output in outputs
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "search_files",
            "folder_path": "KNIOps",
            "query": "proposal",
            "mime_type": "application/vnd.google-apps.document",
            "item_count": 1,
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "read_doc",
            "document_id": "docKBA1",
            "title": "Selected internal document",
            "char_count": 412,
            "truncated": False,
            "send_enabled": False,
        },
    ]
    assert "private-file-id" not in json.dumps(receipts)
    assert "private document body" not in json.dumps(receipts)


@pytest.mark.parametrize(
    ("agent_text", "expected_route", "expected_agent_name", "expected_output_type"),
    [
        ("rss context agent", "rss_context_agent", "rss_context_agent", "RssContextResult"),
        (
            "preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
    ],
)
def test_cli_ask_feed_context_mentions_use_context_dry_run(
    capsys,
    tmp_path: Path,
    agent_text: str,
    expected_route: str,
    expected_agent_name: str,
    expected_output_type: str,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'feed-context.db'}",
            "--json",
            "--input",
            (
                f"@KNI {agent_text}: diagnostic case feed context. "
                "Use available feed context, not browser automation or live web search. "
                "Return a concise Answer and Detailed Summary."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "done"
    assert payload["route"] == expected_route
    assert payload["selected_agent"] == expected_route
    assert payload["output_type"] == expected_output_type
    assert payload["output"]["agent_name"] == expected_agent_name
    assert payload["human_summary"].startswith("*Answer:*\n")
    assert "\n\n*Detailed Summary:*\n" in payload["human_summary"]
    assert "work_item" not in payload


@pytest.mark.parametrize(
    ("agent_text", "expected_route", "expected_agent_name", "expected_output_type"),
    [
        ("rss context agent", "rss_context_agent", "rss_context_agent", "RssContextResult"),
        (
            "business agents rss context agent",
            "rss_context_agent",
            "rss_context_agent",
            "RssContextResult",
        ),
        (
            "preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
        (
            "business agents preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
    ],
)
def test_cli_ask_bare_feed_context_aliases_use_context_dry_run(
    capsys,
    tmp_path: Path,
    agent_text: str,
    expected_route: str,
    expected_agent_name: str,
    expected_output_type: str,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'bare-feed-context.db'}",
            "--json",
            "--input",
            (
                f"{agent_text}: diagnostic case feed context. "
                "Use available feed context, not browser automation or live web search. "
                "Return a concise Answer and Detailed Summary."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "done"
    assert payload["route"] == expected_route
    assert payload["selected_agent"] == expected_route
    assert payload["output_type"] == expected_output_type
    assert payload["output"]["agent_name"] == expected_agent_name
    assert payload["human_summary"].startswith("*Answer:*\n")
    assert "\n\n*Detailed Summary:*\n" in payload["human_summary"]


def test_cli_ask_feed_context_text_mode_prints_human_summary(capsys, tmp_path: Path) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'feed-context-text.db'}",
            "--input",
            (
                "@KNI preprints context agent: diagnostic case feed context. "
                "Use local dry-run preprints context, not external research. "
                "Look for adolescent depression digital phenotyping or wearable-sensor monitoring."
            ),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Preprints Context Agent" in output
    assert "*Answer:*" in output
    assert "*Detailed Summary:*" in output
    assert "Dry-run selected the specialist" in output


def test_feed_context_topic_terms_ignore_instruction_words() -> None:
    terms = cli._feed_context_topic_terms(
        "diagnostic case diag_preprints. Use only local/dry-run preprints context. "
        "Do not research externally. Look for recent preprint context relevant to "
        "adolescent depression digital phenotyping or wearable-sensor monitoring. "
        "Do not create, update, export, write files, send, schedule, publish, create "
        "CRM records, or post elsewhere."
    )

    assert "externally" not in terms
    assert "recent" not in terms
    assert "dry-run" not in terms
    assert "export" not in terms
    assert "research" not in terms
    assert "relevant" not in terms
    assert "concise" not in terms
    assert "human-useful" not in terms
    assert "handoff" not in terms
    assert "whether" not in terms
    assert "adolescent depression" in terms
    assert "digital phenotyping" in terms
    assert "wearable-sensor monitoring" in terms
    assert terms[:3] == [
        "adolescent depression",
        "digital phenotyping",
        "wearable-sensor monitoring",
    ]


def test_feed_context_topic_terms_prioritize_rss_domain_phrases() -> None:
    terms = cli._feed_context_topic_terms(
        "diagnostic case diag_rss. Use only local/dry-run announcements or RSS context. "
        "Do not research externally. Look for recent announcement context relevant to "
        "clinical AI validation, remote monitoring implementation, and operations "
        "dashboard governance. Return a concise human-useful context handoff."
    )

    assert terms[:3] == [
        "clinical AI validation",
        "remote monitoring implementation",
        "operations dashboard governance",
    ]
    assert "announcements" not in terms
    assert "implementation" not in terms
    assert "remote monitoring" not in terms
    assert "externally" not in terms


def test_cli_google_workspace_context_live_sdk_enables_live_read_default(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'google-context-agent.db'}"

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "google_workspace_context_agent"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run_typed_sdk_sync(agent, prompt, output_type, **kwargs):
        captured["agent_name"] = agent.name
        captured["live_reads_env"] = os.environ.get(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV)
        return (
            SimpleNamespace(final_output=None, usage=None),
            cli.GoogleWorkspaceContextResult(
                mode="llm",
                summary="KNIOps Drive context was read with live read defaults.",
                relevant_folders=["KNIOps"],
                relevant_docs=["Operations Doc"],
                relevant_sheets=["KNIOps Structured Data"],
                recommended_target="KNIOps",
                recommended_actions=["Hand context to Chief of Staff."],
            ),
        )

    monkeypatch.delenv(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV, raising=False)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)

    exit_code = main(
        [
            "ask",
            "--agent",
            "google_workspace_context_agent",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            "Read-only Google Workspace context test for KNIOps.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        "agent_name": "google_workspace_context_agent",
        "live_reads_env": "true",
    }
    assert os.environ.get(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV) is None
    assert payload["selected_agent"] == "google_workspace_context_agent"
    assert payload["output_type"] == "GoogleWorkspaceContextResult"


def test_context_agent_human_summary_separates_answer_from_details() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "KNIOps Drive is accessible in read-only mode.",
            "approval_needs": ["Live writes require explicit approval reference"],
            "blockers": ["No Google Sheets were returned in the current folder listing"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "KNIOps Drive is accessible in read-only mode.\n\n"
        "*Detailed Summary:*\n"
        "- Approval/write boundary: Live writes require explicit approval reference.\n"
        "- Needs attention: No Google Sheets were returned in the current folder listing."
    )


def test_work_item_result_shape_honors_exact_two_sentence_summary() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="Research: Example Health",
            request_text=(
                "Summarize Example Health in exactly 2 sentences using fixture context only."
            ),
            sources=[
                WorkItemSourceRef(
                    source_id="fixture:example-health",
                    title="Fixture record for Example Health",
                    provider="fixture",
                    supported_claim="Fixture input identifies the company as Example Health.",
                )
            ],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Verbose synthesized workflow review.",
        manual_request_plan={
            "ask_shape": {
                "output_form": "brief",
                "strict_filter_mode": "exact",
                "stop_condition": "stop_after_exact_requested_sentence_count",
            }
        },
    )

    shaped = cli._shape_work_item_result_for_requested_output(result)

    assert shaped.human_summary == (
        "Fixture input identifies the company as Example Health. "
        "No additional company details are supported by the bounded evidence supplied "
        "for this run."
    )
    assert shaped.next_action is None


def test_context_agent_human_summary_renders_workspace_artifact_preview_lines() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Prepared a read-only two-line outline preview.",
            "recommended_target": "Future Google Doc outline",
            "artifact_preview_lines": [
                "Review behavioral-health AI evidence.",
                "Prepare source-bounded client recommendations.",
            ],
        }
    )

    assert "*Answer:*\nPrepared a read-only two-line outline preview." in summary
    assert "- Draft artifact preview:" in summary
    assert "  - Review behavioral-health AI evidence." in summary
    assert "  - Prepare source-bounded client recommendations." in summary


def test_context_agent_human_summary_honors_exact_bullet_output_shape() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Prepared a two-bullet outline.",
            "recommended_target": "Future Google Doc outline",
            "artifact_preview_lines": [
                "- Review behavioral-health AI evidence",
                "- Prepare client recommendations",
            ],
            "recommended_actions": ["Paste the bullets into a document."],
        },
        {
            "ask_shape": {
                "output_form": "bullets",
                "strict_filter_mode": "exact",
                "stop_condition": "Produce exactly two bullet points and stop.",
            }
        },
    )

    assert summary == (
        "- Review behavioral-health AI evidence\n"
        "- Prepare client recommendations"
    )
    assert "Detailed Summary" not in summary
    assert "Candidate target" not in summary
    assert "Paste the bullets" not in summary


def test_context_agent_human_summary_honors_exact_zotero_item_shape() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Read-only cached Zotero item listed successfully.",
            "article_titles": ["Signal in Noise"],
            "zotero_item_keys": ["KICWQFH4"],
            "sources": [{"title": "Local Zotero Cache"}],
        },
        {
            "ask_shape": {
                "output_form": "unspecified",
                "strict_filter_mode": "exact",
                "stop_condition": (
                    "Stop after returning one available cached Zotero item with title "
                    "and item key."
                ),
            }
        },
    )

    assert summary == "Title: Signal in Noise\nItem key: KICWQFH4"
    assert "Useful references" not in summary


def test_context_agent_human_summary_does_not_deterministically_rewrite_word_cap() -> None:
    long_summary = " ".join(f"word{index}" for index in range(1, 61))
    summary = cli._context_agent_human_summary(
        {
            "summary": long_summary,
            "article_titles": ["Provider-backed article"],
            "zotero_item_keys": ["INTERNAL-KEY"],
            "recommended_actions": ["Review another article."],
        },
        {
            "objective": (
                "Provide its exact title and summarize the abstract in no more than "
                "50 words."
            ),
            "ask_shape": {
                "output_form": "brief",
                "strict_filter_mode": "exact",
                "stop_condition": "stop_after_50_word_summary",
            },
        },
    )

    assert summary == f"Title: Provider-backed article\nSummary: {long_summary}"
    assert long_summary in summary
    assert len(long_summary.split()) == 60
    assert "INTERNAL-KEY" not in summary
    assert "Title: Provider-backed article" in summary
    assert "Review another article" not in summary


def test_strict_requested_display_text_honors_brief_reply_assessment() -> None:
    summary = cli._strict_requested_display_text(
        {
            "summary": "The sender asks to meet Tuesday at 2 PM.",
            "needs_reply": True,
            "reasoning": "A response should confirm availability.",
            "recommended_action": "Draft a reply for review.",
        },
        {
            "ask_shape": {
                "output_form": "brief",
                "strict_filter_mode": "unspecified",
                "stop_condition": (
                    "Stop after producing a one-sentence summary and reply-needed assessment."
                ),
            }
        },
    )

    assert summary == "The sender asks to meet Tuesday at 2 PM.\nReply needed: Yes."
    assert "Draft a reply" not in summary


def test_strict_requested_display_text_includes_requested_copyable_draft() -> None:
    summary = cli._strict_requested_display_text(
        {
            "summary": "The sender shared a certification application.",
            "reasoning": (
                "A response is optional but useful to clarify the most important "
                "readiness criterion before applying."
            ),
            "needs_reply": True,
            "draft_reply": (
                "Thanks for sending this. What readiness criterion would you "
                "recommend confirming before we apply?"
            ),
        },
        {
            "objective": (
                "Tell me briefly what warrants a response, then draft a concise "
                "response here in Slack so I can copy it."
            ),
            "ask_shape": {
                "output_form": "brief",
                "strict_filter_mode": "exact",
                "stop_condition": "Stop after preparing one concise copyable reply.",
            },
        },
    )

    assert summary == (
        "A response is optional but useful to clarify the most important readiness "
        "criterion before applying.\n\n"
        "*Draft response:*\n"
        "Thanks for sending this. What readiness criterion would you recommend "
        "confirming before we apply?"
    )


def test_strict_draft_output_preserves_optional_assessment_and_copyable_reply() -> None:
    summary = cli._strict_requested_display_text(
        {
            "summary": "The sender shared certification information.",
            "reasoning": "A response is optional because no direct question was asked.",
            "needs_reply": False,
            "draft_reply": (
                "Thanks for sharing this. Is there a recommended starting point "
                "for assessing eligibility?"
            ),
        },
        {
            "objective": (
                "Tell me whether it merits a response, then return a short reply "
                "suggestion here so I can paste it."
            ),
            "ask_shape": {
                "output_form": "draft",
                "strict_filter_mode": "unspecified",
                "stop_condition": "",
            },
        },
    )

    assert summary == (
        "A response is optional because no direct question was asked.\n\n"
        "*Draft response:*\n"
        "Thanks for sharing this. Is there a recommended starting point for "
        "assessing eligibility?"
    )


def test_strict_requested_display_text_honors_company_word_limit() -> None:
    summary = cli._strict_requested_display_text(
        {
            "company_name": "Abridge",
            "answer": (
                "Abridge provides ambient clinical documentation AI that converts "
                "clinician-patient conversations into structured notes integrated "
                "directly with health-system electronic record workflows."
            ),
            "facts": [
                {
                    "text": (
                        "Abridge is a generative AI company for clinical documentation "
                        "and ambient listening in healthcare systems worldwide today."
                    ),
                    "source_ids": ["source:1"],
                }
            ],
            "why_it_matters": "This longer generic section must not replace the answer.",
        },
        {
            "objective": "Identify Abridge and summarize the company in 20 words.",
            "constraints": ["brief answer", "20 words maximum"],
            "ask_shape": {
                "output_form": "brief",
                "strict_filter_mode": "flexible",
                "stop_condition": (
                    "Return a 20-word company summary after identifying Abridge."
                ),
            },
        },
    )

    assert summary.startswith("Abridge provides ambient clinical documentation AI")
    assert len(summary.split()) == 20
    assert "worldwide today" not in summary
    assert "generic section" not in summary


def test_strict_requested_display_text_preserves_safety_details() -> None:
    output = {
        "summary": "Prepared a two-bullet outline.",
        "artifact_preview_lines": ["First", "Second"],
        "blockers": ["The selected source could not be verified"],
    }
    plan = {
        "ask_shape": {
            "output_form": "bullets",
            "strict_filter_mode": "exact",
            "stop_condition": "Produce exactly two bullet points and stop.",
        }
    }

    assert cli._strict_requested_display_text(output, plan) == ""
    rendered = cli._context_agent_human_summary(output, plan)
    assert "Needs attention" in rendered
    assert "could not be verified" in rendered


def test_google_workspace_context_dry_run_tracks_onboarding_topic_without_eval_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "google_workspace_context_agent",
        (
            "@KNI google workspace context agent: Use only Google Workspace/Drive context "
            "if available, read-only. I am looking for whether KNIOps has internal "
            "onboarding or operations SOP docs for a new collaborator handoff. "
            "Do not create, edit, share, export, or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "internal onboarding" in payload["summary"]
    assert "operations sop" in payload["summary"].lower()
    assert "Slack eval review narrative" not in payload["summary"]
    assert "Eval tracker" not in payload["summary"]
    assert payload["recommended_target"] == "KNIOps Operations onboarding/SOP context"
    assert "KNIOps/Operations" in payload["relevant_folders"]
    assert "onboarding or operations SOP Doc" in payload["relevant_docs"]
    assert "*Detailed Summary:*" in summary
    assert "- Candidate target: KNIOps Operations onboarding/SOP context" in summary
    assert "- Folder hints:" in summary
    assert "- Doc hints:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require live Workspace reads" in summary


def test_airtable_context_dry_run_tracks_finance_tax_topic_without_eval_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "airtable_context_agent",
        (
            "@KNI airtable context agent: Use Airtable context tools only, read-only. "
            "I am looking for whether the 2026 Finance & Tax Tracker Tax Payments "
            "table has Estimated Tax Period 2 payment records and the Q2 Rolling Taxes "
            "Summary. Do not create, update, delete, attach files, export, or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "2026 finance & tax tracker" in payload["summary"].lower()
    assert "tax payments" in payload["summary"].lower()
    assert "estimated tax period 2" in payload["summary"].lower()
    assert "eval_tracker" not in payload["summary"]
    assert "Promptfoo" not in payload["summary"]
    assert payload["base_alias"] == "finance_tax_tracker"
    assert payload["relevant_tables"] == ["Tax Payments"]
    assert "Estimated Tax Periods" in payload["relevant_fields"]
    assert "Q2 Rolling Taxes Summary" in payload["recommended_record_identity"]
    assert "*Detailed Summary:*" in summary
    assert "- Airtable target hints: finance_tax_tracker / Tax Payments" in summary
    assert "- Field hints:" in summary
    assert "- Record filter guidance:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require live Airtable schema/record reads" in summary


def test_context_agent_human_summary_keeps_multiple_detail_items_readable() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Airtable records were read successfully.",
            "blockers": [
                "Sample records were arbitrary because no filter was provided",
                "One field label differs from the schema label",
            ],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Airtable records were read successfully.\n\n"
        "*Detailed Summary:*\n"
        "- Needs attention:\n"
        "  - Sample records were arbitrary because no filter was provided\n"
        "  - One field label differs from the schema label"
    )


def test_context_agent_human_summary_includes_airtable_record_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Three matching Tax Payments records were visible.",
            "record_summaries": [
                {
                    "key": "IRS Estimated Taxes: Q2 2026 (pending)",
                    "value": "Tax Type: Federal; Amount: $4,705.00; Payment Date: 6/1/2026",
                    "note": "Period 2",
                },
                {
                    "key": "PA Estimated Taxes: Period 2 2026",
                    "value": "Tax Type: State; Amount: $920.00; Payment Date: 6/1/2026",
                    "note": "Period 2",
                },
            ],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Three matching Tax Payments records were visible.\n\n"
        "*Detailed Summary:*\n"
        "- Records visible:\n"
        "  - IRS Estimated Taxes: Q2 2026 (pending): Tax Type: Federal; "
        "Amount: $4,705.00; Payment Date: 6/1/2026 (Period 2)\n"
        "  - PA Estimated Taxes: Period 2 2026: Tax Type: State; "
        "Amount: $920.00; Payment Date: 6/1/2026 (Period 2)"
    )


def test_context_agent_human_summary_keeps_airtable_provider_ids_internal() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Created, updated, and removed one marked test record.",
            "base_alias": "finance_tax_tracker",
            "relevant_tables": ["Business Expenses"],
            "recommended_record_identity": "recProviderInternal123",
            "record_summaries": [
                {
                    "key": "recProviderInternal123",
                    "value": "Business Expenses test record updated and removed.",
                    "note": "Provider read-back and absence verification passed.",
                }
            ],
            "sources": [
                {
                    "title": "Airtable verified test deletion",
                    "note": "Confirmed the marked record was absent after deletion.",
                    "location": "Business Expenses / recProviderInternal123",
                },
                {
                    "title": "Airtable base schema",
                    "note": "Verified the table before writing.",
                    "location": "finance_tax_tracker / appProviderInternal123",
                },
            ],
        }
    )

    assert "recProviderInternal123" not in summary
    assert "appProviderInternal123" not in summary
    assert "Record filter guidance" not in summary
    assert "Business Expenses test record updated and removed" in summary
    assert "Airtable verified test deletion" in summary


def test_context_agent_human_summary_includes_zotero_reference_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "The KNI foundational collection is available.",
            "sources": [
                {
                    "title": "The growing field of digital psychiatry",
                    "note": "Directly relevant to digital psychiatry background.",
                    "location": "https://doi.org/10.1002/wps.20883",
                },
                {
                    "title": "Toward the future of psychiatric diagnosis",
                    "note": "Directly relevant to psychiatric diagnosis background.",
                    "location": "https://doi.org/10.1186/1741-7015-11-126",
                },
            ],
            "blockers": ["No full-text extraction for some book records in the local cache"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "The KNI foundational collection is available.\n\n"
        "*Detailed Summary:*\n"
        "- Needs attention: No full-text extraction for some book records in the local cache.\n\n"
        "*Useful references:*\n"
        "  - The growing field of digital psychiatry - Directly relevant to digital "
        "psychiatry background. (https://doi.org/10.1002/wps.20883)\n"
        "  - Toward the future of psychiatric diagnosis - Directly relevant to "
        "psychiatric diagnosis background. (https://doi.org/10.1186/1741-7015-11-126)"
    )


def test_zotero_context_dry_run_tracks_requested_topic_without_generic_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "zotero_context_agent",
        (
            "@KNI zotero context agent: Use only local Zotero/cache context if available. "
            "I am looking for foundational depression or psychiatric diagnosis review "
            "material for an internal background scan. Do not import or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "foundational depression" in payload["summary"]
    assert "psychiatric diagnosis" in payload["summary"]
    assert "import" not in payload["summary"]
    assert "collections" not in payload["summary"]
    assert "google" not in payload["summary"].lower()
    assert "looking" not in payload["summary"].lower()
    assert "behavioral-health AI validation" not in payload["summary"]
    assert "foundational depression" in payload["collection_hints"]
    assert "psychiatric diagnosis" in payload["collection_hints"]
    assert "*Detailed Summary:*" in summary
    assert "- Collection/context hints:" in summary
    assert "- Evidence guidance:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require local item reads" in summary


@pytest.mark.parametrize(
    "request_text, expected_blocker",
    [
        (
            "Attach this PDF to the article.",
            "Ordinary Zotero PDF attachment is not supported",
        ),
        (
            "Add this link to the notes for the article.",
            "Ordinary Zotero child-note creation or modification is not supported",
        ),
    ],
)
def test_zotero_context_dry_run_reports_specific_missing_mutation_tool(
    request_text: str,
    expected_blocker: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="zotero_context_agent")

    output = cli._context_agent_dry_run_output(
        "zotero_context_agent",
        request_text,
        plan,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    assert expected_blocker in payload["blockers"][0]
    assert "blocked at the tool boundary" in payload["summary"]
    assert payload["zotero_write_supported"] is False


def test_context_agent_human_summary_includes_feed_item_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Two announcement feed items matched the request.",
            "articles": [
                {
                    "title": "AI model validation guide",
                    "url": "https://example.test/ai-validation",
                    "published_at": "2026-06-18",
                    "summary": "Practical validation guidance for clinical AI monitoring.",
                },
                {
                    "title": "Remote monitoring pilot update",
                    "source": "Internal RSS",
                    "summary": "Pilot operations update relevant to dashboard review.",
                },
            ],
            "recurring_themes": ["clinical AI validation", "remote monitoring operations"],
            "evidence_gaps": ["No full article extraction was available for one item"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Two announcement feed items matched the request.\n\n"
        "*Detailed Summary:*\n"
        "- Items reviewed:\n"
        "  - AI model validation guide: Practical validation guidance for clinical AI "
        "monitoring. (2026-06-18)\n"
        "  - Remote monitoring pilot update: Pilot operations update relevant to dashboard "
        "review. (Internal RSS)\n"
        "- Themes:\n"
        "  - clinical AI validation\n"
        "  - remote monitoring operations\n"
        "- Needs attention: No full article extraction was available for one item.\n\n"
        "*Useful references:*\n"
        "  - AI model validation guide (https://example.test/ai-validation)"
    )


def test_cli_ask_gmail_triage_long_prompt_is_not_treated_as_fixture_path(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    database_url = f"sqlite:///{tmp_path / 'gmail-ask.db'}"
    long_prompt = (
        "gmail triage agent: diagnostic case diag_gmail_live_receipt_20260618_002 "
        "Use only this inline, non-sensitive email context: Subject: Partnership "
        "follow-up for remote patient monitoring validation From: Alex Rivera, "
        "Partnerships Lead, Example Health Body: Thanks for the earlier discussion. "
        "We are evaluating whether Keystone could help review our remote patient "
        "monitoring AI validation workflow before a pilot proposal in July. Could "
        "you summarize whether this needs a reply and draft-only next-step suggestion? "
        "No PHI is included. Return a human-useful Gmail triage answer: priority, "
        "why it matters, whether a reply is needed, suggested next action, and any "
        "caveats. Do not send email, create a Gmail draft, label messages, schedule, "
        "write files, create CRM records, publish, or post elsewhere."
    )

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "gmail_triage"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "EmailTriageResult",
                    "send_enabled": False,
                    "model": {"provider": "openai", "name": "gpt-5.4-mini"},
                    "usage": {"requests": 1, "total_tokens": 42},
                    "cost": {"estimated_usd": 0.001},
                    "output": {
                        "subject": "Partnership follow-up for remote patient monitoring validation",
                        "priority": "high",
                        "summary": "A reply is needed.",
                        "needs_reply": True,
                        "recommended_action": "Prepare a draft-only reply.",
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "gmail_triage",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            long_prompt,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert payload["output_type"] == "EmailTriageResult"
    assert payload["model_execution"]["model"] == "gpt-5.4-mini"
    assert payload["agent_run_id"]
    assert calls
    assert calls[0][1] == "scripts/run_gmail_triage.py"
    assert "--fixture" in calls[0]
    assert "--live-gmail" not in calls[0]
    assert "--no-live-gmail" in calls[0]
    assert "--no-allow-inbox" in calls[0]
    assert "--request" in calls[0]
    assert calls[0][calls[0].index("--request") + 1] == long_prompt
    assert "--compact-instructions" in calls[0]
    records = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(records) == 1
    assert records[0]["agent_name"] == "gmail_triage"
    assert records[0]["dry_run"] == 0


def test_cli_ask_live_explicit_mention_honors_manual_plan_reroute(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityScoutResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "three-company comparison",
                        "ranked_opportunities": [],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "business",
            "research",
            "analyst",
            "Compare",
            "three",
            "software-first",
            "companies",
            "with",
            "measurement-based",
            "care",
            "or",
            "digital",
            "psychiatry",
            "tools",
            "for",
            "behavioral",
            "health",
            "clinics.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "opportunity_scout"
    assert payload["manual_request_plan"]["target_agent"] == "opportunity_scout"
    assert calls
    command = calls[0]
    assert "scripts/run_opportunity_scout.py" in command
    assert "--topic" in command
    topic = command[command.index("--topic") + 1]
    assert topic == (
        "Compare three software-first companies with measurement-based care or digital "
        "psychiatry tools for behavioral health clinics."
    )
    assert command[command.index("--max-results") + 1] == "3"


def test_cli_ask_cost_tracking_directive_is_recorded_without_reaching_child(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    preflight_inputs: list[str] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        preflight_inputs.append(str(request_text))
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--json",
            "research",
            "Lindus.",
            "Also",
            "keep",
            "track",
            "of",
            "this",
            "run",
            "costs.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cost_tracking_requested"] is True
    assert preflight_inputs == ["research Lindus"]
    assert calls
    assert "Also keep track" not in " ".join(calls[0])


def test_cli_ask_live_payload_surfaces_missing_information(
    monkeypatch,
    capsys,
) -> None:
    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {
                        "summary": "partial",
                        "unknowns": ["I did not have source-backed leadership evidence."],
                        "limitations": ["Only one source was available."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Missing information:" in output
    assert "source-backed leadership evidence" in output
    assert "Only one source was available" in output


def test_cli_ask_kni_explicit_agent_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "opportunity_scout"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityScoutResult",
                    "send_enabled": False,
                    "output": {"records": []},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "@KNI", "opportunity", "scout", "find", "AI", "partners"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Opportunity Scout Agent" in output
    assert "Output type: OpportunityScoutResult" in output
    assert "Orchestrator review:" in output
    assert "WorkItem:" not in output
    assert calls
    assert "scripts/run_opportunity_scout.py" in calls[0]
    assert "--live-search" in calls[0]
    assert "--live-search-plan" not in calls[0]
    assert "--live-sdk" in calls[0]
    assert "--compact-instructions" in calls[0]
    assert calls[0][calls[0].index("--topic") + 1] == "find AI partners"


def test_cli_explicit_scout_company_summary_runs_business_research_owner(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "agent_name": "business_research_analyst",
                    "output_type": "CompanyResearchFocusedBrief",
                    "send_enabled": False,
                    "output": {
                        "company_name": "Abridge",
                        "product": "Abridge provides ambient clinical documentation software.",
                        "sources": [{"url": "https://www.abridge.com"}],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "opportunity",
            "scout",
            "who",
            "is",
            "Abridge",
            "and",
            "summarize",
            "the",
            "company",
            "in",
            "20",
            "words.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "business_research_analyst"
    assert payload["manual_request_plan"]["requested_agent"] == "opportunity_scout"
    assert payload["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["manual_request_plan"]["primary_target"] == "Abridge"
    assert payload["manual_request_plan"]["required_entities"] == ["Abridge"]
    assert payload["manual_request_plan"]["ask_shape"]["stop_condition"] == (
        "stop_after_20_word_summary"
    )
    assert calls
    assert "scripts/run_company_research.py" in calls[0]
    assert calls[0][calls[0].index("--company") + 1] == "Abridge"
    assert calls[0][calls[0].index("--max-results") + 1] == "2"
    assert "--quick-retrieval" in calls[0]
    assert "--live-search-plan" not in calls[0]
    assert "--compact-instructions" in calls[0]
    assert "scripts/run_opportunity_scout.py" not in calls[0]


def test_cli_ask_explicit_chief_of_staff_runs_orchestrator_preflight_advise_only(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    child_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        child_envs.append(dict(kwargs.get("env") or {}))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "Architecture review for agent routing.",
                        "recommended_actions": ["Keep raw request visible to specialists."],
                        "audit_notes": ["No external write attempted."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "chief",
            "of",
            "staff",
            "review",
            "the",
            "agent",
            "architecture",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["orchestrator_preflight"]["advisory_only"] is True
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["orchestrator_preflight"]["route_result"]["route"] == "chief_of_staff"
    assert calls
    assert "scripts/run_chief_of_staff.py" in calls[0]
    assert "--live-search" not in calls[0]
    assert "--live-search-plan" not in calls[0]
    assert child_envs
    assert ORCHESTRATOR_PREFLIGHT_ENV in child_envs[0]
    assert MANUAL_REQUEST_PLAN_ENV in child_envs[0]
    assert ORCHESTRATOR_ROUTE_RESULT_ENV in child_envs[0]


def test_cli_ask_explicit_agent_send_request_blocked_by_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "blocked"
    assert payload["status"] == "blocked"
    assert payload["send_enabled"] is False
    assert payload["orchestrator_preflight"]["blocked_by_orchestrator"] is True
    assert payload["orchestrator_preflight"]["execution_allowed"] is False
    assert payload["block_kind"] == "send"
    assert payload["orchestrator_preflight"]["route_result"]["refused"] is True
    assert calls == []


def test_cli_ask_preflight_blocked_omits_raw_workflow_state(
    monkeypatch,
    capsys,
) -> None:
    def fake_preflight_with_slack_state(*args, **kwargs):
        preflight = _fake_orchestrator_preflight(*args, **kwargs)
        workflow_state_summary = preflight.route_result.workflow_state_summary.__class__.model_validate(
            {
                "recent_slack_thread": [{"summary": "private Slack refusal context"}],
                "prior_agent_runs": [{"summary": "prior operator correction"}],
            }
        )
        preflight.route_result = preflight.route_result.model_copy(
            update={"workflow_state_summary": workflow_state_summary}
        )
        return preflight

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight_with_slack_state)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 0
    assert payload["status"] == "blocked"
    assert "workflow_state_summary" not in payload_text
    assert "private Slack refusal context" not in payload_text
    assert "prior operator correction" not in payload_text


def test_cli_ask_gmail_reply_without_thread_context_is_blocked(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "gmail_triage",
            "--live-sdk",
            "--json",
            "Reply politely and confirm next week works.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "missing_gmail_context"
    assert payload["requires_gmail_context"] is True
    assert payload["send_enabled"] is False
    assert payload["agent_execution_plan"]["operation"] == "draft_reply"
    assert "usable email context" in payload["message"]
    assert "pasted sanitized email" in payload["message"]
    assert calls == []


def test_cli_exact_operator_gmail_draft_write_propagates_scoped_approval(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "Find the latest email from Example Health and create a Gmail draft reply. "
        "Do not send it."
    )
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "operator@example.com")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert len(calls) == 1
    command = calls[0]
    assert "--live-gmail" in command
    assert "--live-sdk" in command
    assert "--create-draft" in command
    assert "--no-dry-run" in command
    assert command[command.index("--expected-account") + 1] == "operator@example.com"
    approval_reference = command[command.index("--approval-reference") + 1]
    assert approval_reference.startswith("operator-command:gmail-draft:")
    assert prompt not in approval_reference
    assert command[command.index("--gmail-query") + 1].endswith('"example health"')
    assert payload["agent_execution_plan"]["create_gmail_drafts"] is True
    assert payload["send_enabled"] is False


def test_cli_natural_gmail_draft_update_resolves_without_operator_id(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "Update the existing Gmail draft with subject 'Project follow-up' for "
        "reviewer@example.com to be shorter and warmer. Do not send."
    )
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "operator@example.com")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    command = calls[0]
    assert "--update-draft" in command
    assert "--create-draft" not in command
    assert command[command.index("--draft-subject-hint") + 1] == "Project follow-up"
    assert command[command.index("--draft-recipient-hint") + 1] == (
        "reviewer@example.com"
    )
    assert "--approval-reference" in command
    assert "--expected-account" in command
    assert payload["agent_execution_plan"]["operation"] == "update_draft"
    assert payload["agent_execution_plan"]["draft_subject_hint"] == "Project follow-up"
    assert payload["send_enabled"] is False


def test_cli_ask_gmail_triage_live_promotes_inline_email_workflow_to_work_item(
    monkeypatch,
    capsys,
) -> None:
    work_item_calls: list[dict[str, object]] = []

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    prompt = (
        "gmail triage this sanitized inbound email from Mindful Care, research "
        "Mindful Care, and prepare a draft-only Slack-thread sample outreach for "
        "review. Email: From: Jordan Lee, Operations at Mindful Care. Subject: "
        "Follow-up on measurement support. Body: Hi Jordan, our team is reviewing "
        "measurement-based care workflows and may need advisory help on evaluation "
        "design. Could you let me know if this is relevant for Keystone? Do not "
        "send, create Gmail drafts, post outside this thread, schedule, publish, "
        "create files, update Airtable/CRM/Drive/Sheets, or write external systems."
    )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert len(work_item_calls) == 1
    call = work_item_calls[0]
    assert call["input_text"] == prompt
    assert call["live_search"] is False
    assert call["live_sdk"] is True
    assert call["json_output"] is True
    assert call["max_manager_steps"] == 3
    assert call["manual_plan"] is not None
    assert call["orchestrator_preflight"] is not None


def test_cli_explicit_orchestrator_source_bundle_uses_work_item_graph(
    monkeypatch,
    capsys,
) -> None:
    work_item_calls: list[dict[str, object]] = []
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)
    monkeypatch.setattr(
        cli,
        "_run_ask_orchestrator",
        lambda *_args, **_kwargs: pytest.fail("direct Orchestrator path must not run"),
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--context-file",
            str(fixture_path),
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert len(work_item_calls) == 1
    call = work_item_calls[0]
    assert call["context_file_path"] == str(fixture_path)
    assert call["live_sdk"] is True
    assert call["live_search"] is False
    assert call["max_manager_steps"] == 3


def test_cli_source_bundle_request_budget_allows_planner_then_blocks_resolved_route(
    monkeypatch,
    capsys,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    preflight_calls: list[str] = []

    def bounded_preflight(request_text, **kwargs):
        preflight_calls.append(request_text)
        result = _fake_orchestrator_preflight(request_text, **kwargs)
        return result.model_copy(
            update={"sdk_usage_events": [{"usage": {"requests": 1}}]}
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", bounded_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--context-file",
            str(fixture_path),
            "--max-openai-requests",
            "1",
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == [
        "Research the supplied packet and prepare a draft-only reply."
    ]
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["openai_requests_made"] == 1
    assert payload["estimated_requests"]["max"] == 2
    assert payload["estimated_requests"]["stages"] == [
        "manual_request_planner",
        "outreach_composer_synthesis",
    ]


def test_cli_source_bundle_no_live_manual_plan_fits_one_request_ceiling(
    monkeypatch,
    capsys,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    work_item_calls: list[dict[str, object]] = []
    preflight_live_plan_values: list[bool] = []

    def fake_preflight(request_text, *, live_manual_plan, **kwargs):
        preflight_live_plan_values.append(live_manual_plan)
        return _fake_orchestrator_preflight(
            request_text,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--no-live-manual-plan",
            "--context-file",
            str(fixture_path),
            "--max-openai-requests",
            "1",
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert preflight_live_plan_values == [False]
    assert len(work_item_calls) == 1


def test_cli_connector_backed_gmail_graph_budget_blocks_before_preflight(
    monkeypatch,
    capsys,
) -> None:
    preflight_calls: list[str] = []
    request = (
        "Read the latest Gmail thread from alex@example.test, research Example Health "
        "using only the selected thread context, and prepare a draft reply for review "
        "without sending, creating a Gmail draft, searching the web, posting, or "
        "writing externally."
    )

    def unexpected_preflight(request_text, **_kwargs):
        preflight_calls.append(request_text)
        pytest.fail("connector graph budget must block before preflight or Gmail")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", unexpected_preflight)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--no-live-manual-plan",
            "--max-openai-requests",
            "0",
            "--max-manager-steps",
            "3",
            "--json",
            request,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == []
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["estimated_requests"]["min"] == 4
    assert payload["estimated_requests"]["stages"] == [
        "gmail_provider_read",
        "business_research_sdk",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]
    assert payload["estimated_requests"]["max"] == 7
    assert payload["openai_requests_made"] == 0


def test_bounded_connector_graph_fits_eight_request_ceiling_and_disables_web_search() -> None:
    request = (
        "Read the latest Gmail thread from the configured exact test sender, research "
        "the sender organization using only the selected thread context, and return "
        "a suggested reply with supporting evidence and the approval status."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert estimate["max"] == 8
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_provider_read",
        "business_research_sdk",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]


def test_direct_opportunity_scout_uses_shared_live_manual_plan() -> None:
    request = (
        "Find current remote-accessible opportunities relevant to Keystone across "
        "conferences, workshops, certifications, grants, collaborations, consulting, "
        "and professional networking."
    )
    args = SimpleNamespace(
        context_file="",
        agent="opportunity_scout",
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "opportunity_scout_direct_sdk",
    ]


@pytest.mark.parametrize(
    ("route", "request_text"),
    [
        (
            "business_research_analyst",
            "Summarize one supplied local source in 50 words without web search.",
        ),
        (
            "opportunity_scout",
            "Assess one supplied opportunity and return one recommendation.",
        ),
        (
            "outreach_composer",
            "Draft one short reply from approved context and do not send it.",
        ),
        (
            "gmail_triage",
            "Summarize one selected email and do not modify Gmail.",
        ),
        (
            "airtable_context_agent",
            "Read one exact Airtable record and do not modify it.",
        ),
        (
            "google_workspace_context_agent",
            "Read one exact Google Doc and summarize it.",
        ),
        (
            "zotero_context_agent",
            "Read one Zotero abstract and summarize it in 50 words.",
        ),
        (
            "rss_context_agent",
            "Read one RSS announcement and summarize it.",
        ),
        (
            "preprints_context_agent",
            "Read one preprint abstract and summarize it.",
        ),
    ],
)
def test_bounded_direct_specialists_use_compact_request_estimate(
    route: str,
    request_text: str,
) -> None:
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request_text,
        live_sdk=True,
        live_manual_plan=True,
        requested_route=route,
    )

    conditional_repair = "words" in request_text
    assert estimate["max"] == 3 + int(conditional_repair)
    assert estimate["min"] == 2
    assert estimate["stages"] == [
        "manual_request_planner",
        f"{route}_direct_sdk",
        *(["conditional_instruction_following_repair"] if conditional_repair else []),
    ]


def test_preacquired_zotero_direct_read_estimates_one_specialist_request() -> None:
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )
    request = (
        "BA use Zotero to select the most recently added journal article with a "
        "stored abstract and summarize it in 50 words."
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="business_research_analyst",
    )

    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "zotero_context_agent_direct_sdk",
        "conditional_instruction_following_repair",
    ]
    assert "counts model turns only" in estimate["note"]
    assert "provider tool calls are not OpenAI requests" in estimate["note"]


@pytest.mark.parametrize(
    ("request_text", "expected_class", "expected_compact"),
    [
        (
            "Summarize one selected email and do not modify Gmail.",
            "bounded_read",
            True,
        ),
        (
            "Update one exact Airtable record and verify the same record.",
            "bounded_write",
            True,
        ),
        (
            "For the same Zotero article, list the authors and publication venue.",
            "thread_followup",
            True,
        ),
        (
            "Run a comprehensive multi-stage landscape across all available sources.",
            "deep_or_multistage",
            False,
        ),
    ],
)
def test_direct_runtime_profile_follows_request_shape_after_route_selection(
    request_text: str,
    expected_class: str,
    expected_compact: bool,
) -> None:
    profile = cli._direct_specialist_runtime_profile(
        "business_research_analyst",
        input_text=request_text,
    )

    assert profile["request_class"] == expected_class
    assert profile["compact_instructions"] is expected_compact


@pytest.mark.parametrize(
    ("route", "request_text"),
    [
        (
            "airtable_context_agent",
            "In Airtable, create KBA_TEST_RECORD_VALIDATION, change it, then delete it.",
        ),
        (
            "zotero_context_agent",
            "In Zotero, create a KBA_TEST_NOTE_VALIDATION note, revise it, then remove it.",
        ),
        (
            "google_workspace_context_agent",
            "Create a Google Doc titled KBA_TEST_DOC_VALIDATION, verify it, then "
            "move the document to trash.",
        ),
        (
            "google_workspace_context_agent",
            "Make a Google Doc named KBA_TEST_DOC_VARIATION, confirm it, then place "
            "that document in Drive trash.",
        ),
    ],
)
def test_marked_composite_lifecycle_keeps_compact_direct_profile(
    route: str,
    request_text: str,
) -> None:
    profile = cli._direct_specialist_runtime_profile(route, input_text=request_text)

    assert profile["request_class"] == "bounded_write"
    assert profile["bounded_composite_lifecycle"] is True
    assert profile["compact_instructions"] is True
    assert cli._direct_specialist_request_estimate(
        route,
        input_text=request_text,
        live_search=False,
    ) == 2


def test_marked_doc_body_copy_does_not_change_lifecycle_operations() -> None:
    request = (
        "Create a Google Doc titled KBA_TEST_DOC_BODY_COPY in KNIOps with the body "
        "“Set the update policy after review.” Verify it, then move that same document "
        "to Drive trash."
    )

    assert cli._is_bounded_composite_lifecycle_request(
        "google_workspace_context_agent",
        input_text=request,
    )


def test_google_doc_lifecycle_scope_recovers_same_body_from_slack_history() -> None:
    latest = (
        "google workspace context agent rerun that exact approved "
        "KBA_TEST_DOC_COS_0718 lifecycle now with the same body, verify it, then "
        "move only that same Google Doc to Drive trash."
    )
    envelope = (
        "google workspace context agent continue this prior Slack thread. "
        "Previous request: CoS create one Google Doc titled KBA_TEST_DOC_COS_0718 "
        "in KNIOps with the body “Created through a natural CoS lifecycle ask.” "
        "User follow-up: "
        f"{latest} Continue the same agent task."
    )

    assert cli._google_doc_lifecycle_scope(latest, context_text=envelope) == (
        "KBA_TEST_DOC_COS_0718",
        "Created through a natural CoS lifecycle ask.",
        "KNIOps",
    )


def test_google_doc_lifecycle_scope_accepts_human_sentence_wording() -> None:
    request = (
        "Leave one Google document in KNIOps called KBA_TEST_DOC_SENTENCE_SCOPE "
        "with the sentence ‘This checks a natural CoS document task.’ Check that "
        "it opens, then file only that test document in Drive trash."
    )

    assert cli._google_doc_lifecycle_scope(request, context_text=request) == (
        "KBA_TEST_DOC_SENTENCE_SCOPE",
        "This checks a natural CoS document task.",
        "KNIOps",
    )


def test_provider_lifecycle_public_receipt_redacts_provider_identity() -> None:
    result = cli._public_lifecycle_receipt(
        {
            "status": "success",
            "record_id": "rec-private",
            "provider_link": "https://provider.example/rec-private",
            "verification": {"passed": True, "draft_id": "draft-private"},
        }
    )

    assert result == {
        "status": "success",
        "verification": {"passed": True},
    }


def test_bounded_provider_lifecycle_dispatches_to_interpreted_owner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_doc_runner(input_text: str, **kwargs: object) -> int:
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(cli, "_run_direct_google_doc_test_lifecycle", fake_doc_runner)
    request = (
        "Create Google Doc KBA_TEST_DOC_DISPATCH in KNIOps, verify it, and trash "
        "that same document."
    )

    result = cli._run_bounded_provider_lifecycle_after_preflight(
        "google_workspace_context_agent",
        request,
        context_text=request,
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        database_url="sqlite:///:memory:",
    )

    assert result == 17
    assert captured["input_text"] == request
    assert captured["context_text"] == request


def test_llm_objective_can_bind_equivalent_lifecycle_phrasing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_doc_runner(input_text: str, **kwargs: object) -> int:
        captured["input_text"] = input_text
        return 23

    monkeypatch.setattr(cli, "_run_direct_google_doc_test_lifecycle", fake_doc_runner)
    request = (
        "Take the marked Google Doc KBA_TEST_DOC_EQUIVALENT in KNIOps through "
        "the full approved lifecycle and clean it up."
    )
    plan = ManualRequestPlan(
        source="live_sdk",
        requested_agent="chief_of_staff",
        target_agent="google_workspace_context_agent",
        intent="business_system_write",
        objective=(
            "Create KBA_TEST_DOC_EQUIVALENT, verify it, then move that same "
            "Google Doc to trash."
        ),
    )
    interpreted_scope = cli._interpreted_lifecycle_scope_text(request, plan)

    assert not cli._is_bounded_composite_lifecycle_request(
        "google_workspace_context_agent",
        input_text=request,
    )
    assert cli._is_bounded_composite_lifecycle_request(
        "google_workspace_context_agent",
        input_text=interpreted_scope,
    )
    assert (
        cli._run_bounded_provider_lifecycle_after_preflight(
            "google_workspace_context_agent",
            request,
            lifecycle_scope_text=interpreted_scope,
            context_text=request,
            json_output=True,
            manual_plan=plan,
            orchestrator_preflight=None,
            database_url="sqlite:///:memory:",
        )
        == 23
    )
    assert captured["input_text"] == request


@pytest.mark.parametrize(
    ("operator_text", "target_route", "objective", "runner_name"),
    [
        (
            "Please take marked Airtable record KBA_TEST_RECORD_SEMANTIC through "
            "its full approved lifecycle and leave no test residue.",
            "airtable_context_agent",
            "Create KBA_TEST_RECORD_SEMANTIC, verify it, update that same Airtable "
            "record, verify it again, then delete it and confirm absence.",
            "_run_direct_airtable_test_record_lifecycle",
        ),
        (
            "Please take Google Doc KBA_TEST_DOC_SEMANTIC in KNIOps through its "
            "full approved lifecycle using the approved body and leave no test residue.",
            "google_workspace_context_agent",
            "Create Google Doc KBA_TEST_DOC_SEMANTIC in KNIOps, verify it, then move "
            "that same document to Drive trash and confirm it is trashed.",
            "_run_direct_google_doc_test_lifecycle",
        ),
        (
            "Please take Gmail draft KBA_TEST_DRAFT_SEMANTIC through its full "
            "approved lifecycle without sending it and leave no test residue.",
            "gmail_triage",
            "Create Gmail draft KBA_TEST_DRAFT_SEMANTIC, verify it, update that same "
            "draft, verify it again, then delete it and confirm absence without sending.",
            "_run_direct_gmail_test_draft_lifecycle",
        ),
    ],
)
def test_kni_cos_semantic_lifecycle_runs_typed_helper_after_one_request_preflight(
    operator_text: str,
    target_route: str,
    objective: str,
    runner_name: str,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **_kwargs,
    ):
        captured["preflight_request"] = request_text
        captured["requested_agent"] = requested_agent
        captured["live_manual_plan"] = live_manual_plan
        plan = ManualRequestPlan(
            source="live_sdk",
            requested_agent=requested_agent,
            target_agent=target_route,
            intent="business_system_write",
            objective=objective,
        )
        route_result = cli.route_request(request_text, manual_plan=plan)
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent=target_route,
            manual_request_plan=plan,
            route_result=route_result,
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_lifecycle_runner(input_text: str, **kwargs: object) -> int:
        captured["lifecycle_input"] = input_text
        captured["lifecycle_kwargs"] = kwargs
        return 0

    def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("semantic marked lifecycle must not fall through to agent or WorkItem")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, runner_name, fake_lifecycle_runner)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", unexpected_fallback)
    monkeypatch.setattr(cli, "_run_ask_work_item", unexpected_fallback)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "1",
            "--json",
            f"@KNI CoS {operator_text}",
        ]
    )

    assert exit_code == 0
    assert captured["preflight_request"] == operator_text
    assert captured["requested_agent"] == "chief_of_staff"
    assert captured["live_manual_plan"] is True
    assert captured["lifecycle_input"] == operator_text
    lifecycle_kwargs = captured["lifecycle_kwargs"]
    assert isinstance(lifecycle_kwargs, dict)
    assert lifecycle_kwargs["manual_plan"].target_agent == target_route


def test_ordinary_chief_budget_allows_planner_then_blocks_resolved_route(
    monkeypatch,
    capsys,
) -> None:
    calls: list[str] = []

    def bounded_preflight(request_text, **kwargs):
        calls.append(request_text)
        result = _fake_orchestrator_preflight(request_text, **kwargs)
        return result.model_copy(
            update={"sdk_usage_events": [{"usage": {"requests": 1}}]}
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", bounded_preflight)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "1",
            "--json",
            "@KNI CoS review current operations and recommend my top three actions",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert calls == ["review current operations and recommend my top three actions"]
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["openai_requests_made"] == 1


def test_postposed_only_note_uses_compact_chief_request_estimate() -> None:
    request = (
        "CoS: From this note only, Northstar Care sells referral-navigation software "
        "and has no audited outcomes. Give me one supported fact and the first "
        "validation question. No search or writes."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert estimate["min"] == 2
    assert estimate["max"] == 2
    assert estimate["stages"] == [
        "manual_request_planner",
        "chief_of_staff_response_only_sdk",
    ]


def test_kni_cos_doc_lifecycle_continuation_recovers_same_body_before_direct_helper(
    monkeypatch,
) -> None:
    raw_slack_envelope = (
        "business agents continue this prior Slack thread. "
        "Previous request: @KNI CoS create one Google Doc titled "
        "KBA_TEST_DOC_COS_THREAD in KNIOps with the body "
        "“Created through a natural CoS lifecycle ask.” Verify it, then move only "
        "that same Google Doc to Drive trash. "
        "Previous result title: Business Agents Google Workspace Blocked "
        "User follow-up: @KNI CoS rerun that exact approved KBA_TEST_DOC_COS_THREAD "
        "lifecycle now with the same body, verify it, then move only that same Google "
        "Doc to Drive trash. Continue the same agent task."
    )
    expected_followup = (
        "rerun that exact approved KBA_TEST_DOC_COS_THREAD lifecycle now with the "
        "same body, verify it, then move only that same Google Doc to Drive trash."
    )
    captured: dict[str, object] = {}

    def fake_preflight(
        request_text,
        *,
        requested_agent=None,
        **_kwargs,
    ):
        plan = ManualRequestPlan(
            source="live_sdk",
            requested_agent=requested_agent,
            target_agent="google_workspace_context_agent",
            intent="business_system_write",
            objective=(
                "Create Google Doc KBA_TEST_DOC_COS_THREAD in KNIOps, verify it, "
                "then move that same document to Drive trash and confirm it is trashed."
            ),
        )
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent="google_workspace_context_agent",
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_doc_runner(input_text: str, **kwargs: object) -> int:
        captured["input_text"] = input_text
        captured["context_text"] = kwargs["context_text"]
        captured["scope"] = cli._google_doc_lifecycle_scope(
            input_text,
            context_text=str(kwargs["context_text"]),
        )
        return 0

    def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("same-object lifecycle continuation must stay on the direct helper")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_direct_google_doc_test_lifecycle", fake_doc_runner)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", unexpected_fallback)
    monkeypatch.setattr(cli, "_run_ask_work_item", unexpected_fallback)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "1",
            "--json",
            raw_slack_envelope,
        ]
    )

    assert exit_code == 0
    assert captured["input_text"] == expected_followup
    assert captured["context_text"] == raw_slack_envelope
    assert captured["scope"] == (
        "KBA_TEST_DOC_COS_THREAD",
        "Created through a natural CoS lifecycle ask.",
        "KNIOps",
    )


def test_kni_cos_semantic_doc_lifecycle_uses_verified_wrapper_and_public_redaction(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    operator_text = (
        "Please take Google Doc KBA_TEST_DOC_WRAPPER in KNIOps through its full "
        "approved lifecycle using the body “Natural CoS wrapper validation.” and "
        "leave no test residue."
    )
    database_url = f"sqlite:///{tmp_path / 'doc-wrapper.sqlite'}"
    captured: dict[str, object] = {}

    def fake_preflight(request_text, *, requested_agent=None, **_kwargs):
        plan = ManualRequestPlan(
            source="live_sdk",
            requested_agent=requested_agent,
            target_agent="google_workspace_context_agent",
            intent="business_system_write",
            objective=(
                "Create Google Doc KBA_TEST_DOC_WRAPPER in KNIOps, verify it, then "
                "move that same document to Drive trash and confirm it is trashed."
            ),
        )
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent="google_workspace_context_agent",
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_doc_lifecycle(title: str, body_text: str, **kwargs: object):
        captured["title"] = title
        captured["body_text"] = body_text
        captured.update(kwargs)
        return {
            "status": "success",
            "operation": "test_doc_lifecycle",
            "document_id": "doc-private-123",
            "provider_link": "https://docs.google.com/document/d/doc-private-123/edit",
            "approval_reference": str(kwargs["approval_reference"]),
            "create": {
                "status": "success",
                "document_id": "doc-private-123",
                "verification": {"passed": True},
            },
            "trash": {
                "status": "success",
                "document_id": "doc-private-123",
                "verification": {"passed": True},
            },
            "verification": {
                "passed": True,
                "create_read_back": True,
                "document_trashed_after_cleanup": True,
            },
            "send_enabled": False,
        }

    def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("verified Doc lifecycle must not fall through to an agent or WorkItem")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "google_doc_test_lifecycle_impl", fake_doc_lifecycle)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", unexpected_fallback)
    monkeypatch.setattr(cli, "_run_ask_work_item", unexpected_fallback)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "1",
            "--database-url",
            database_url,
            "--json",
            f"@KNI CoS {operator_text}",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 0
    assert captured["title"] == "KBA_TEST_DOC_WRAPPER"
    assert captured["body_text"] == "Natural CoS wrapper validation."
    assert captured["folder_path"] == "KNIOps"
    assert captured["live"] is True
    assert str(captured["approval_reference"]).startswith(
        "operator-command:google-workspace-test-lifecycle:"
    )
    assert payload["status"] == "done"
    assert payload["route"] == "google_workspace_context_agent"
    assert payload["output_type"] == "GoogleWorkspaceContextResult"
    assert payload["output"]["summary"] == payload["human_summary"]
    assert payload["slack_display_text"] == payload["human_summary"]
    assert payload["slack_display_title"].endswith("Lifecycle Complete")
    assert payload["openai_requests"] == 1
    assert payload["provider_identity_redacted"] is True
    assert payload["tool_receipt"]["verification"]["passed"] is True
    assert payload["side_effects"] == {
        "google_doc_created_and_verified": True,
        "google_doc_trashed_after_cleanup": True,
        "slack_message_posted": False,
    }
    assert "doc-private-123" not in payload_text
    assert "docs.google.com" not in payload_text
    assert "WorkItem command completed" not in payload_text
    assert "approval_reference" not in payload["tool_receipt"]

    stored_rows = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(stored_rows) == 1
    assert stored_rows[0]["agent_name"] == "google_workspace_context_agent"
    assert stored_rows[0]["status"] == "success"
    assert stored_rows[0]["dry_run"] == 0
    assert "doc-private-123" in stored_rows[0]["output_json"]


def test_kni_cos_semantic_gmail_lifecycle_uses_verified_wrapper_without_send(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    operator_text = (
        "Please take Gmail draft KBA_TEST_DRAFT_WRAPPER through its full approved "
        "lifecycle without sending it and leave no test residue."
    )
    database_url = f"sqlite:///{tmp_path / 'gmail-wrapper.sqlite'}"
    captured: dict[str, object] = {}

    def fake_preflight(request_text, *, requested_agent=None, **_kwargs):
        plan = ManualRequestPlan(
            source="live_sdk",
            requested_agent=requested_agent,
            target_agent="gmail_triage",
            intent="business_system_write",
            objective=(
                "Create Gmail draft KBA_TEST_DRAFT_WRAPPER, verify it, update that "
                "same draft, verify it again, then delete it and confirm absence "
                "without sending."
            ),
        )
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent="gmail_triage",
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    class FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            captured["gmail_live"] = live
            self.live = live

    def fake_gmail_lifecycle(_gmail, **kwargs: object):
        captured.update(kwargs)
        return {
            "status": "success",
            "operation": "test_draft_lifecycle",
            "draft_id": "draft-private-123",
            "approval_reference": str(kwargs["approval_reference"]),
            "create": {
                "status": "ok",
                "draft_id": "draft-private-123",
                "verification": {"passed": True},
            },
            "update": {
                "status": "ok",
                "draft_id": "draft-private-123",
                "verification": {"passed": True},
            },
            "delete": {
                "status": "ok",
                "draft_id": "draft-private-123",
                "verification": {"passed": True},
            },
            "verification": {
                "passed": True,
                "create_read_back": True,
                "same_draft_update_read_back": True,
                "draft_absent_after_cleanup": True,
            },
            "sent": False,
            "send_enabled": False,
        }

    def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("verified Gmail lifecycle must not fall through to an agent or WorkItem")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_configured_gmail_draft_account", lambda: "operator@example.test")
    monkeypatch.setattr(cli, "GmailTool", FakeGmailTool)
    monkeypatch.setattr(cli, "execute_gmail_test_draft_lifecycle", fake_gmail_lifecycle)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", unexpected_fallback)
    monkeypatch.setattr(cli, "_run_ask_work_item", unexpected_fallback)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--max-openai-requests",
            "1",
            "--database-url",
            database_url,
            "--json",
            f"@KNI CoS {operator_text}",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 0
    assert captured["gmail_live"] is True
    assert captured["marker"] == "KBA_TEST_DRAFT_WRAPPER"
    assert captured["expected_account"] == "operator@example.test"
    assert captured["recipient"] == "operator@example.test"
    assert str(captured["approval_reference"]).startswith("operator-command:")
    assert payload["status"] == "done"
    assert payload["route"] == "gmail_triage"
    assert payload["output_type"] == "EmailTriageResult"
    assert payload["output"]["summary"] == payload["human_summary"]
    assert payload["slack_display_text"] == payload["human_summary"]
    assert payload["slack_display_title"].endswith("Lifecycle Complete")
    assert payload["openai_requests"] == 1
    assert payload["provider_identity_redacted"] is True
    assert payload["send_enabled"] is False
    assert payload["tool_receipt"]["verification"]["passed"] is True
    assert payload["side_effects"] == {
        "gmail_draft_created": True,
        "gmail_draft_absent_after_cleanup": True,
        "email_sent": False,
        "slack_message_posted": False,
    }
    assert "draft-private-123" not in payload_text
    assert "WorkItem command completed" not in payload_text
    assert "approval_reference" not in payload["tool_receipt"]

    stored_rows = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(stored_rows) == 1
    assert stored_rows[0]["agent_name"] == "gmail_triage"
    assert stored_rows[0]["status"] == "success"
    assert stored_rows[0]["dry_run"] == 0
    assert "draft-private-123" in stored_rows[0]["output_json"]


def test_named_ba_zotero_read_uses_compact_source_owner_estimate() -> None:
    request = (
        "Use Zotero to select the most recently added journal article with a stored "
        "abstract. Provide its exact title and summarize the abstract in no more than "
        "50 words. Do not use web search, full text, or modify Zotero."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="business_research_analyst",
    )

    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "zotero_context_agent_direct_sdk",
        "conditional_instruction_following_repair",
    ]
    assert "counts model turns only" in estimate["note"]


def test_named_ba_zotero_read_runs_preflight_then_source_owner(
    monkeypatch,
) -> None:
    request = (
        "Use Zotero to select the most recently added journal article with a stored "
        "abstract. Provide its exact title and summarize the abstract in no more than "
        "50 words. Do not use web search, full text, or modify Zotero."
    )
    captured: dict[str, object] = {}

    def fake_preflight(request_text, *, requested_agent=None, live_manual_plan=False, **kwargs):
        captured["preflight_request"] = request_text
        captured["requested_agent"] = requested_agent
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_specialist(route, input_text, **kwargs):
        captured["specialist_route"] = route
        captured["specialist_input"] = input_text
        captured["manual_plan"] = kwargs["manual_plan"]
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_specialist_live", fake_specialist)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--live-sdk",
            "--max-openai-requests",
            "8",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert captured["preflight_request"] == request
    assert captured["requested_agent"] == "business_research_analyst"
    assert captured["live_manual_plan"] is True
    assert captured["specialist_route"] == "zotero_context_agent"
    assert captured["specialist_input"] == request
    plan = captured["manual_plan"]
    assert isinstance(plan, ManualRequestPlan)
    assert plan.ask_shape.stop_condition == "stop_after_50_word_summary"


def test_slack_continuation_recovers_latest_named_agent_followup() -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Linked WorkItem: wi_example Previous request: workitem business agents "
        "continue this prior Slack thread. Previous request: &gt; BA old request. "
        "Previous result title: Business Agents WorkItem Failed "
        "User follow-up: BA use Zotero to select the most recently added journal "
        "article with a stored abstract. Provide its exact title and summarize the "
        "abstract in no more than 50 words. Do not use web search, full text, or "
        "modify Zotero. Continue the same agent task."
    )

    operator_request = cli._latest_slack_operator_request(request)
    mention = cli.parse_agent_mention(
        operator_request,
        allow_bare_agent_aliases=True,
    )

    assert operator_request.startswith("BA use Zotero")
    assert "Previous result" not in operator_request
    assert mention.route == "business_research_analyst"
    assert mention.input_text.startswith("use Zotero")


def test_slack_continuation_without_history_file_keeps_latest_ask_and_prior_object() -> None:
    request = (
        "ba continue this prior Slack thread. "
        "Previous request: BA use Zotero to select the most recently added journal "
        "article with a stored abstract. "
        "Previous result title: Business Agents Zotero Context Ready "
        "Previous result: Title: Using AI to Detect Psychosis Relapse: Scoping Review. "
        "Summary: The stored abstract was summarized. "
        "User follow-up: Who are the authors, where was the article published, and "
        "summarize its abstract in no more than 100 words. Use the same Zotero article "
        "from this thread. Do not use web search, full text, or modify Zotero. "
        "Continue the same agent task."
    )

    assert cli._latest_slack_operator_request(request) == (
        "business research analyst Who are the authors, where was the article published, "
        "and summarize its abstract in no more than 100 words. Use the same Zotero "
        "article from this thread. Do not use web search, full text, or modify Zotero."
    )
    state = cli._slack_continuation_workflow_state(request)
    assert state["prior_agent_runs"] == [
        {
            "id": "slack-envelope-1",
            "route": "zotero_context_agent",
            "status": "completed",
            "title": "Using AI to Detect Psychosis Relapse: Scoping Review.",
            "summary": (
                "Title: Using AI to Detect Psychosis Relapse: Scoping Review. Summary: "
                "The stored abstract was summarized."
            ),
        }
    ]


def test_slack_continuation_state_merge_deduplicates_prior_results() -> None:
    prior = {
        "route": "zotero_context_agent",
        "title": "Provider article",
        "summary": "Stored abstract summary.",
    }

    merged = cli._merge_direct_workflow_state(
        {"slack_context": {"thread_ts": "123.456"}, "prior_agent_runs": [prior]},
        {"prior_agent_runs": [dict(prior)]},
    )

    assert merged["slack_context"] == {"thread_ts": "123.456"}
    assert merged["prior_agent_runs"] == [prior]


def test_slack_operator_request_strips_rendered_quote_and_lone_mention_marker() -> None:
    assert cli._latest_slack_operator_request(
        "&gt; @ BA summarize one stored abstract"
    ) == "BA summarize one stored abstract"


def test_slack_continuation_preserves_named_chief_route_for_path_followup() -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: add this personal expense in Airtable. "
        "User follow-up: The path to the receipt is /private/tmp/receipt.pdf. "
        "Continue the same agent task."
    )

    operator_request = cli._latest_slack_operator_request(request)
    mention = cli.parse_agent_mention(
        operator_request,
        allow_bare_agent_aliases=True,
    )

    assert operator_request.startswith("chief of staff The path")
    assert mention.route == "chief_of_staff"
    assert mention.input_text.startswith("The path")


@pytest.mark.parametrize(
    ("outer_agent", "newest_ask", "expected_route"),
    [
        (
            "BA",
            "CoS audit the current request and identify the right internal owner.",
            "chief_of_staff",
        ),
        (
            "CoS",
            "Gmail triage review the unread messages from today.",
            "gmail_triage",
        ),
        (
            "OS",
            "BA research the company named in the selected message.",
            "business_research_analyst",
        ),
        (
            "BA",
            "OS find current behavioral-health partnership opportunities.",
            "opportunity_scout",
        ),
        (
            "BA",
            "GWC read the selected Google Doc and summarize it.",
            "google_workspace_context_agent",
        ),
    ],
)
def test_slack_continuation_newest_explicit_agent_supersedes_outer_route(
    outer_agent: str,
    newest_ask: str,
    expected_route: str,
) -> None:
    request = (
        f"{outer_agent} continue this prior Slack thread. "
        "Previous request: research the earlier object. "
        "Previous result title: Business Agents Company Research Ready "
        "Previous result: Title: Earlier object. Summary: Prior result. "
        f"User follow-up: {newest_ask} Continue the same agent task."
    )

    operator_request = cli._latest_slack_operator_request(request)
    mention = cli._parse_ask_agent_mention(
        operator_request,
        slack_context_input=False,
        slack_continuation=True,
    )

    assert operator_request == newest_ask
    assert mention.explicit is True
    assert mention.route == expected_route
    assert "Previous result" not in mention.input_text


def test_slack_continuation_newest_bare_cos_runs_current_route_not_stale_graph(
    tmp_path: Path,
    capsys,
) -> None:
    request = (
        "BA continue this prior Slack thread. "
        "Previous request: research the earlier object. "
        "Previous result title: Business Agents Company Research Ready "
        "Previous result: Title: Earlier object. Summary: Prior result. "
        "User follow-up: CoS audit the current request and identify the right "
        "internal owner. Continue the same agent task."
    )

    exit_code = main(
        [
                "ask",
                "--database-url",
                f"sqlite:///{tmp_path / 'continuation-route.db'}",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["orchestrator_preflight"]["selected_agent"] == "chief_of_staff"
    assert payload["manual_request_plan"]["requested_agent"] == "chief_of_staff"
    assert payload["manual_request_plan"]["target_agent"] == "chief_of_staff"
    assert payload["route"] == "chief_of_staff"
    assert payload["status"] == "done"
    assert payload["work_item"]["request_text"] == (
        "research the earlier object.\n"
        "Prior result for context: Title: Earlier object. Summary: Prior result.\n"
        "Authoritative follow-up: audit the current request and identify the right "
        "internal owner."
    )
    assert payload["_execution"]["openai_requests"] == 0
    assert "Previous result" not in payload["work_item"]["request_text"]


def test_chief_format_only_followup_preserves_prior_facts_without_outreach_reroute(
    tmp_path: Path,
    capsys,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: I need a quick prep note. Using only these facts, give me "
        "exactly three concise bullets: all entry surfaces should create the same "
        "request envelope; direct and stateful multi-agent execution may remain "
        "separate backends; a provider write is complete only after its receipt passes "
        "verification. Do not search, call providers, or change anything. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: A longer note. "
        "User follow-up: Make that just the three bullets with no note after them. "
        "Continue the same agent task."
    )

    exit_code = main(
        [
            "ask",
            "--database-url",
            f"sqlite:///{tmp_path / 'format-followup.db'}",
            "--no-live-sdk",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["target_agent"] == "chief_of_staff"
    assert payload["manual_request_plan"]["intent"] == "route_request"
    assert payload["human_summary"] == (
        "- All entry surfaces should create the same request envelope.\n"
        "- Direct and stateful multi-agent execution may remain separate backends.\n"
        "- A provider write is complete only after its receipt passes verification."
    )
    assert payload["public_result"]["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert "clarification" not in payload["human_summary"].lower()
    assert "slack.com" not in payload["human_summary"].lower()


@pytest.mark.parametrize("route", ["orchestrator", "chief_of_staff"])
def test_coordinating_agents_keep_broader_request_estimates(route: str) -> None:
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text="Coordinate a multi-stage operational review.",
        live_sdk=True,
        live_manual_plan=True,
        requested_route=route,
    )

    assert estimate["max"] > 3
    assert estimate["stages"][0] == "manual_request_planner"


def test_prior_context_chief_response_only_request_fits_five_request_ceiling() -> None:
    request = (
        "CoS, one more correction: reply with only the exact three bullets "
        "supported by my original request. No heading or closing note. Do not "
        "search, call tools or providers, draft anything, or change anything."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
    )

    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "chief_of_staff_response_only_sdk",
        "conditional_instruction_following_repair",
    ]


def test_natural_cos_supplied_note_graph_counts_only_model_backed_stages() -> None:
    request = (
        "CoS, I have five minutes before a partnership discussion. Here is all I know: "
        "Harbor Bridge Health sells behavioral-health care-navigation software to "
        "health plans and says it tracks referral completion and care engagement, but "
        "it has not shared audited outcomes, customer references, implementation data, "
        "or an evaluation design. Give me one decision brief: what is actually "
        "supported, the strongest potential KNI advisory or research fit, the single "
        "validation question that should come first, and a short internal Slack note I "
        "can paste to the team. Use only this note; do not search, create or modify "
        "anything, draft or send email, or post anywhere else."
    )
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=4,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
    )

    assert estimate["min"] == 3
    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "business_research_analyst_source_provided_deterministic",
        "opportunity_scout_source_provided_deterministic",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    plan.ask_shape.output_constraints = (
        plan.ask_shape.output_constraints.model_copy(
            update={
                "interpretation": (
                    "Return a decision brief and a separate paste-ready Slack note."
                ),
                "required_sections": [
                    "Decision brief",
                    "Paste-ready Slack note",
                ],
                "require_section_headings": True,
            }
        )
    )
    resolved = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert resolved["min"] == 3
    assert resolved["max"] == 4
    assert resolved["stages"][-1] == "conditional_instruction_following_repair"


def test_short_human_cos_stateful_review_fits_shared_five_request_ceiling() -> None:
    request = (
        "CoS: Track this review. Northstar Care sells referral-navigation software "
        "but has no audited outcomes. Assess what is supported, choose the first "
        "validation gap, and give me a paste-ready internal Slack recommendation. "
        "No search or external actions."
    )
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=4,
        live_search=False,
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert estimate["min"] == 3
    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "business_research_analyst_source_provided_deterministic",
        "opportunity_scout_source_provided_deterministic",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]


def test_natural_cos_supplied_note_without_planner_workflow_stays_bounded() -> None:
    request = (
        "CoS, I have a partner check-in shortly. Here is the only information I "
        "have: Riverbend Behavioral is considering a clinician-facing documentation "
        "assistant for outpatient psychiatry practices and says it may reduce "
        "after-hours charting, but it has not provided time-motion data, a safety "
        "evaluation, pilot retention, or customer references. Give me two clearly "
        "separated things. First, a concise decision brief covering what this "
        "establishes and does not establish, the strongest potential KNI fit, and the "
        "first validation question. Second, a paste-ready internal Slack update for "
        "my team in two or three sentences. Use only this note. Do not search, use "
        "provider tools, create or modify anything, draft or send email, or post "
        "anywhere else."
    )
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=4,
        live_search=False,
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.workflow == []
    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert estimate["min"] == 2
    assert estimate["max"] == 2
    assert estimate["stages"] == [
        "manual_request_planner",
        "chief_of_staff_response_only_sdk",
    ]


def test_cos_exact_gmail_read_to_slack_reply_fits_shared_five_request_ceiling() -> None:
    request = (
        'CoS, find the Gmail email with subject "Why Healthtech Needs a New Kind '
        'of Product Leader". Read its complete thread, tell me briefly what warrants '
        "a response, then draft a concise response here in this Slack thread so I "
        "can copy it. Use your judgment: either ask one thoughtful question or make "
        "one useful point. Do not create a Gmail draft, send anything, change labels, "
        "archive, or otherwise modify the mailbox."
    )
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=3,
        live_search=False,
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
    )

    assert cli._route_with_manual_plan_advice("chief_of_staff", plan) == "gmail_triage"
    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_triage_direct_sdk",
    ]
    assert not any(
        "chief_of_staff_sdk" in stage
        or "outreach_composer" in stage
        or "manager_specialist" in stage
        for stage in estimate["stages"]
    )


def test_cos_email_title_and_paste_copy_rephrase_fits_shared_five_request_ceiling() -> None:
    request = (
        'CoS, I need a quick reply I can paste. Find the email titled "RUAIH '
        'Certification", read the whole conversation, and tell me briefly whether it '
        "merits a response. Then write a short reply here—choose either one sensible "
        "question or one useful observation. Leave Gmail exactly as it is: no draft, "
        "send, label changes, or archive."
    )
    args = SimpleNamespace(
        context_file="",
        agent="chief_of_staff",
        max_manager_steps=3,
        live_search=False,
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
    )

    assert cli._route_with_manual_plan_advice("chief_of_staff", plan) == "gmail_triage"
    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_triage_direct_sdk",
    ]


def test_live_cos_exact_gmail_read_executes_gmail_specialist_not_manager_graph(
    monkeypatch,
) -> None:
    request = (
        'CoS, find the Gmail email with subject "Why Healthtech Needs a New Kind '
        'of Product Leader". Read its complete thread, tell me briefly what warrants '
        "a response, then draft a concise response here in this Slack thread so I "
        "can copy it. Do not create a Gmail draft, send anything, change labels, "
        "archive, or otherwise modify the mailbox."
    )
    calls: list[tuple[str, str]] = []

    def fake_gmail_live(input_text: str, **_kwargs: object) -> int:
        calls.append(("gmail_triage", input_text))
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_gmail_triage_live", fake_gmail_live)
    monkeypatch.setattr(
        cli,
        "_run_ask_chief_of_staff_live",
        lambda *_args, **_kwargs: pytest.fail("Chief must delegate this bounded Gmail ask"),
    )
    monkeypatch.setattr(
        cli,
        "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail("single-owner Gmail ask must not use a graph"),
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--max-openai-requests",
            "5",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert calls == [("gmail_triage", request)]


def test_live_gmail_child_promotes_assessment_and_copyable_draft(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    request = (
        'CoS, find the Gmail email with subject "RUAIH Certification". Read that '
        "email in the context of its complete thread. Tell me briefly what warrants "
        "a response, then draft a concise response here in this Slack thread so I "
        "can copy it. Use your judgment: either ask one thoughtful question or make "
        "one useful point, whichever fits the email better. Do not create a Gmail "
        "draft, send anything, change labels, archive, or otherwise modify the mailbox."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    plan.source = "llm"
    plan.objective = (
        'Find the Gmail email with subject "RUAIH Certification", read its complete '
        "thread, identify briefly what warrants a response, and draft a concise "
        "Slack-ready reply the operator can copy."
    )
    plan.ask_shape.output_constraints = InterpretedOutputConstraints(
        interpretation=(
            "Brief Slack-ready response with a short note on what warrants a reply "
            "and one concise draft reply."
        ),
        scope="entire_response",
        style_requirements=["keep it concise", "draft only", "Slack readable"],
    )
    assessment = (
        "A response is optional but useful to clarify the most important readiness "
        "criterion before applying."
    )
    draft = (
        "Thanks for sending this. What readiness criterion would you recommend "
        "confirming before we apply?"
    )
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "EmailTriageResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "The sender shared a certification application.",
                        "reasoning": assessment,
                        "needs_reply": True,
                        "draft_reply": draft,
                    },
                    "model": {
                        "provider": "openai",
                        "name": "gpt-5.4-mini",
                        "run_mode": "live_sdk",
                    },
                }
            ),
            stderr="",
        ),
    )

    exit_code = cli._run_ask_script_live(
        "gmail_triage",
        request,
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / 'gmail-slack-render.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("status") != "blocked"
    assert payload["human_summary"] == f"{assessment}\n\n*Draft response:*\n{draft}"
    assert payload["slack_display_text"] == payload["human_summary"]
    assert payload["instruction_following"]["validation"]["applicable"] is False
    assert payload["instruction_following"]["repair_attempted"] is False


def test_unnamed_supplied_facts_request_resolves_to_bounded_chief_budget() -> None:
    request = (
        "I’m short on time. Without searching or using provider tools, use only "
        "these two facts: negative constraints now narrow execution instead of "
        "blocking it, and the full offline suite passes. Give me exactly two short "
        "bullets: what changed and what we still need to validate. Do not draft "
        "outreach, create or modify records, or include routing metadata."
    )
    plan = infer_manual_request_plan(request)
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route=plan.target_agent,
    )

    assert plan.target_agent == "chief_of_staff"
    assert estimate["max"] == 2
    assert estimate["stages"] == [
        "manual_request_planner",
        "chief_of_staff_direct_supplied_response_sdk",
    ]


def test_bounded_connector_graph_accepts_state_and_collaboration_wording() -> None:
    request = (
        "Review the latest Gmail thread from the configured exact test sender, including "
        "all messages and the original inquiry. Identify the current conversation state, "
        "recommend the most useful KNI-specific collaboration next step using only that "
        "thread and approved KNI context, and include a reply only if replying now would "
        "move the relationship forward."
    )

    assert cli._is_bounded_gmail_recommendation_graph(request, manager_steps=3) is False
    assert cli._is_bounded_gmail_research_reply_graph(request, manager_steps=3) is True
    assert cli._request_forbids_live_research(request) is True


def test_controlled_gmail_recommendation_fits_two_request_ceiling() -> None:
    request = (
        "Review the latest Gmail thread from the configured exact test sender, including "
        "the original inquiry and all messages. Using only that complete thread and "
        "approved KNI context, identify the current conversation state and recommend the "
        "most useful KNI-specific next step. Include reply copy only if replying now would "
        "move the relationship forward, and return the result here for review."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_recommendation_graph(request, manager_steps=3) is True
    assert estimate["max"] == 2
    assert estimate["stages"] == ["manual_request_planner", "outreach_composer_sdk"]


def test_bounded_gmail_research_summary_includes_conditional_repair_ceiling() -> None:
    request = (
        "Read the latest Gmail thread from the configured exact sender, including all "
        "messages. Identify the organization and product, then research them using "
        "current public sources. Determine the underlying data source, distinguish "
        "supported facts from inference, summarize the limitations, and include visible "
        "source links. Do not draft or send a reply, create a Gmail draft, post, schedule, "
        "share, or write externally."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_research_summary_graph(request, manager_steps=3) is True
    assert estimate["max"] == 9
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_provider_read",
        "business_research_sdk",
        "final_response_synthesis",
        "conditional_instruction_following_repair",
    ]


def test_gmail_research_without_explicit_summary_and_no_write_boundary_stays_generic() -> None:
    request = "Read the latest Gmail thread, research the company, and tell me what to do."
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_research_summary_graph(request, manager_steps=3) is False
    assert estimate["max"] == 20


def test_cli_explicit_chief_budget_uses_delegated_context_owner_turn_limit(
    monkeypatch,
    capsys,
) -> None:
    preflight_calls: list[str] = []
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )

    def unexpected_preflight(request_text, **_kwargs):
        preflight_calls.append(request_text)
        pytest.fail("delegated-route request budget must block before preflight")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_CHIEF_OF_STAFF_SDK_MAX_TURNS", "4")
    monkeypatch.delenv("KEYSTONE_AIRTABLE_CONTEXT_AGENT_SDK_MAX_TURNS", raising=False)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", unexpected_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--no-live-manual-plan",
            "--max-openai-requests",
            "4",
            "--json",
            request,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == []
    assert payload["estimated_requests"]["max"] == 6
    assert payload["estimated_requests"]["stages"] == ["airtable_context_agent_sdk"]
    assert payload["openai_requests_made"] == 0


def test_simple_chief_airtable_receipt_uses_direct_context_agent_budget() -> None:
    request = (
        "Add this attached receipt as exactly one personal expense in Airtable. "
        "Read the PDF, map only receipt-backed fields to the live schema, attach "
        "the PDF, and verify the created record and attachment."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="chief_of_staff",
    )

    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "business_system_write"
    assert estimate["max"] == 3
    assert estimate["stages"] == [
        "manual_request_planner",
        "airtable_context_agent_direct_sdk",
    ]


@pytest.mark.parametrize(
    ("prompt", "expected_max"),
    [
        (
            "Read one Personal Expenses record with order KBA_TEST_ORDER_001 and "
            "report its receipt-backed fields.",
            2,
        ),
        (
            "Update one Personal Expenses record with order KBA_TEST_ORDER_001 to set "
            "Description to Software subscription and verify it.",
            3,
        ),
    ],
)
def test_direct_airtable_single_action_budget_is_bounded(
    prompt: str,
    expected_max: int,
) -> None:
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=prompt,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="airtable_context_agent",
    )

    assert estimate["max"] == expected_max
    assert estimate["stages"] == ["airtable_context_agent_direct_sdk"]


def test_cli_ask_gmail_triage_live_accepts_simple_inline_sanitized_email_fixture(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    fixture_texts: list[str] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        fixture_path = Path(command[command.index("--fixture") + 1])
        fixture_texts.append(fixture_path.read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "gmail triage this sanitized inbound email and prepare a draft reply for review. "
        "Email: From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up on "
        "measurement support. Body: Hi Jordan, our team is reviewing measurement-based "
        "care workflows and may need advisory help on evaluation design. Could you let "
        "me know if this is relevant for Keystone? Do not send or create Gmail drafts."
    )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert calls
    command = calls[0]
    assert "--fixture" in command
    assert "--no-live-gmail" in command
    assert "--no-allow-inbox" in command
    assert "--sender-name" in command
    assert command[command.index("--sender-name") + 1] == (
        "Jordan Lee, Operations at Mindful Care"
    )
    assert fixture_texts == [
        (
            "Subject: Follow-up on measurement support\n\n"
            "Hi Jordan, our team is reviewing measurement-based care workflows and "
            "may need advisory help on evaluation design. Could you let me know if "
            "this is relevant for Keystone?"
        )
    ]


def test_cli_ask_live_child_timeout_returns_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def timeout_run(command, **kwargs):
        raise cli.subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", timeout_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "timeout"
    assert payload["timeout_seconds"] == 2.0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "timeout"
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "provider_timeout"


def test_opportunity_scout_child_timeout_reserves_synthesis_headroom(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_RETRIEVAL_DEADLINE_SECONDS", "90")

    assert cli._child_agent_timeout_seconds(route="opportunity_scout") == 150.0
    assert cli._child_agent_timeout_seconds(route="business_research_analyst") == 120.0


def test_explicit_child_timeout_remains_authoritative_for_opportunity_scout(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_RETRIEVAL_DEADLINE_SECONDS", "90")

    assert cli._child_agent_timeout_seconds(route="opportunity_scout") == 75.0


def test_cli_ask_live_child_failure_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def failed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("y" * 20)
        fake_stderr_token = "sk-" + ("x" * 24)
        long_trace_tail = "ValueError: final diagnostic line from SDK provider"
        return SimpleNamespace(
            returncode=7,
            stdout=f"partial stdout token={fake_stdout_token}",
            stderr=(
                f"failed token={fake_stderr_token}\n"
                + "\n".join(f"stack frame {index}" for index in range(500))
                + f"\n{long_trace_tail}"
            ),
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", failed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 7
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 7
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_failed"
    assert payload["output"]["returncode"] == 7
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "unknown_error"
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text
    assert "stack frame 250" not in payload["output"]["stderr_excerpt"]
    assert "ValueError: final diagnostic line from SDK provider" in payload["output"]["stderr_excerpt"]
    assert "ValueError: final diagnostic line from SDK provider" in payload["output"]["error_tail"]
    assert "sk-" + ("x" * 24) not in payload["output"]["error_tail"]


def test_live_child_failure_persists_redacted_local_diagnostics(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'child-failure.db'}"
    fake_token = "sk-" + ("z" * 24)
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                f"Traceback token={fake_token}\n"
                "ModelBehaviorError: structured output did not match the schema"
            ),
        ),
    )

    exit_code = cli._run_ask_script_live(
        "gmail_triage",
        "Find one selected email and draft a Slack-only response.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=infer_manual_request_plan(
            "Find one selected email and draft a Slack-only response.",
            requested_agent="gmail_triage",
        ),
        database_url=database_url,
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_run_id"] > 0
    rows = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "schema_or_parse_error"
    stored = json.loads(rows[0]["output_json"])
    assert (
        "ModelBehaviorError: structured output did not match the schema"
        in stored["output"]["error_tail"]
    )
    assert fake_token not in rows[0]["output_json"]
    assert "[REDACTED]" in rows[0]["output_json"]


def test_cli_ask_live_child_failure_prefers_operator_failure_payload(
    monkeypatch,
    capsys,
) -> None:
    def failed_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "output": {
                        "failure": {
                            "schema": "keystone.operator_failure.v1",
                            "kind": "missing_credentials",
                            "summary": "A required live-provider credential is unavailable.",
                            "reason": "OPENAI_API_KEY=<missing-test-openai-key>",
                            "next_step": "Configure or disable the live provider, then rerun.",
                            "retryable": False,
                            "safe_to_continue": True,
                        }
                    },
                }
            ),
            stderr="Traceback should not become the summary",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", failed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 1
    assert payload["output"]["failure"]["kind"] == "missing_credentials"
    assert payload["output"]["summary"] == "A required live-provider credential is unavailable."
    assert "sk-test-secret" not in payload_text


def test_cli_ask_work_item_failure_returns_clear_json_and_stderr(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    def failed_work_item(*_args, **_kwargs):
        raise ToolGuardrailViolation("blocked by safety guardrail")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(
        "keystone_agents.langgraph_workflow.advance_work_item_manager_loop_with_optional_langgraph",
        failed_work_item,
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'workitems.sqlite'}",
            "@KNI opportunity scout find source-backed grants",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["output"]["error_type"] == "ToolGuardrailViolation"
    assert "safety guardrail" in payload["output"]["summary"]
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "guardrail_block"
    assert payload["output"]["failure"]["safe_to_continue"] is True
    assert "Business Agents run failed" in captured.err
    assert "Traceback" not in captured.err


def test_cli_ask_live_child_malformed_json_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def malformed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("x" * 24)
        fake_stderr_token = "sk-" + ("y" * 20)
        return SimpleNamespace(
            returncode=0,
            stdout=f"not json token={fake_stdout_token}",
            stderr=f"warning token={fake_stderr_token}",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", malformed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_malformed_json"
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "schema_or_parse_error"
    assert "parse_error" in payload["output"]
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text


def test_cli_ask_no_live_sdk_opt_out_keeps_work_item_in_live_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-live-opt-out.db'}"
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "research",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output


def test_operator_no_live_sdk_wording_overrides_live_cli_flag() -> None:
    args = SimpleNamespace(live_sdk=True)

    assert (
        cli._ask_live_sdk_enabled(
            args,
            input_text="Run deterministically with no live SDK or model calls.",
        )
        is False
    )
    assert cli._ask_live_sdk_enabled(args, input_text="Return the OpenAI request count.") is True


def test_cli_agents_list_prints_registry_cards(capsys) -> None:
    exit_code = main(["agents", "list"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "gmail_triage" in output
    assert "OpportunityScoutResult" in output
    assert "Live flags" in output


def test_cli_agents_list_json(capsys) -> None:
    exit_code = main(["agents", "list", "--json"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"route_name": "orchestrator"' in output
    assert '"tool_policy"' in output
    assert '"allowed_tool_names"' in output


def test_cli_agents_tools_prints_sanitized_runtime_availability(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "agent_cards",
        lambda: [
            {
                "route_name": "chief_of_staff",
                "agent_name": "Chief",
                "runtime_tool_availability": {
                    "file_search": {
                        "status": "available",
                        "available": True,
                        "vector_store_id_count": 1,
                        "vector_store_source": "agent",
                        "vector_store_env_name": (
                            "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS"
                        ),
                    },
                    "local_kni_documents": {
                        "status": "ready",
                        "available": True,
                        "indexed_count": 1055,
                        "local_only": True,
                        "model_context_allowed": True,
                        "send_enabled": False,
                    },
                    "mcp": {
                        "status": "sdk_available_not_configured",
                        "available": False,
                        "sdk_available": True,
                        "reason": "Agents SDK HostedMCPTool is installed.",
                    },
                    "source_layer_policy": {
                        "status": "declared",
                        "available": True,
                        "layers": [
                            {
                                "layer": "local_kni_documents",
                                "runtime_status": "ready",
                            },
                            {
                                "layer": "hosted_file_search",
                                "runtime_status": "available",
                            },
                            {
                                "layer": "public_web_search",
                                "runtime_status": "attached_live_available",
                            },
                        ],
                    },
                },
            }
        ],
    )

    exit_code = main(["agents", "tools", "--agent", "chief_of_staff"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "chief_of_staff" in output
    assert "file_search" in output
    assert "local_kni_documents" in output
    assert "stores=1" in output
    assert "indexed=1055" in output
    assert "source_layer_policy" in output
    assert "local_kni_documents:ready" in output
    assert "hosted_file_search:available" in output
    assert "sdk_available_not_configured" in output
    assert "vs_" not in output


def test_cli_agents_tools_json_reports_runtime_availability(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "agent_cards",
        lambda: [
            {
                "route_name": "business_research_analyst",
                "agent_name": "Research",
                "runtime_tool_availability": {
                    "file_search": {
                        "status": "not_configured",
                        "available": False,
                        "vector_store_id_count": 0,
                    }
                },
            }
        ],
    )

    exit_code = main(["agents", "tools", "--agent", "business_research_analyst", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["route_name"] == "business_research_analyst"
    assert payload[0]["runtime_tool_availability"]["file_search"]["status"] == (
        "not_configured"
    )


def test_cli_agents_file_search_config_prints_sanitized_summary(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "local_file_search_config_summary",
        lambda: {
            "path": ".local/file-search-vector-stores.json",
            "exists": True,
            "configured": True,
            "status": "ready",
            "error": None,
            "global": {
                "vector_store_id_count": 1,
                "max_num_results": 5,
                "include_search_results": False,
            },
            "agents": [
                {
                    "agent_name": "chief_of_staff",
                    "known_agent": True,
                    "vector_store_id_count": 1,
                    "max_num_results": None,
                    "include_search_results": True,
                },
                {
                    "agent_name": "unknown_helper",
                    "known_agent": False,
                    "vector_store_id_count": 1,
                    "max_num_results": None,
                    "include_search_results": False,
                },
            ],
            "unknown_agents": ["unknown_helper"],
        },
    )

    exit_code = main(["agents", "file-search-config"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Local FileSearch config: ready" in output
    assert "chief_of_staff" in output
    assert "unknown_helper" in output
    assert "Unknown agents: unknown_helper" in output
    assert "Stores" in output
    assert "vs_" not in output


def test_cli_agents_file_search_config_json_reports_invalid(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "local_file_search_config_summary",
        lambda: {
            "path": ".local/file-search-vector-stores.json",
            "exists": True,
            "configured": True,
            "status": "invalid_config",
            "error": "agents.chief_of_staff.vector_store_ids must include at least one id",
            "global": None,
            "agents": [],
            "unknown_agents": [],
        },
    )

    exit_code = main(["agents", "file-search-config", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "invalid_config"
    assert payload["error"]
