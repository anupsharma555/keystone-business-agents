from __future__ import annotations

import json
import subprocess

from keystone_agents import cli
from keystone_agents.agents.manual_request_planner import _planner_model_configs
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.manual_request import infer_manual_request_plan, merge_manual_request_plan
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def test_manual_plan_routes_conference_search_to_opportunity_scout() -> None:
    plan = infer_manual_request_plan(
        "Find 5 broad behavioral health AI opportunities across conferences "
        "where Keystone could offer implementation oriented presentations.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.target_type == "conference"
    assert plan.desired_count == 5
    assert plan.requires_live_search is True


def test_manual_plan_routes_generic_opportunity_to_outreach_loop_to_workflow() -> None:
    plan = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. "
        "Top 1 only. Post approval to this channel. Draft only, do not send.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_to_outreach_loop"
    assert plan.target_type == "opportunity"
    assert plan.primary_target == "behavioral health AI"
    assert plan.desired_count == 1
    assert plan.requires_live_search is True
    assert "behavioral health" in plan.constraints


def test_manual_plan_preserves_workflow_when_llm_candidate_misreads_company() -> None:
    fallback = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. Top 1 only.",
        requested_agent="orchestrator",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target="AI. Top",
        target_type="company",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.intent == "opportunity_to_outreach_loop"
    assert merged.target_agent == "opportunity_scout"
    assert merged.primary_target == "behavioral health AI"
    assert any("Ignored planner override" in warning for warning in merged.planner_warnings)


def test_manual_plan_specific_agent_call_stays_on_requested_agent() -> None:
    plan = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. Top 1 only.",
        requested_agent="opportunity scout",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"


def test_manual_plan_chief_of_staff_reference_capture_is_not_slack_ops() -> None:
    plan = infer_manual_request_plan(
        (
            "keep this for future reference: NIH AI conference with virtual attendees: "
            "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"
        ),
        requested_agent="chief of staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "reference_capture"
    assert plan.target_type == "operator_reference"
    assert plan.primary_target == "NIH AI conference with virtual attendees"


def test_manual_plan_extracts_business_research_target_from_direct_call() -> None:
    plan = infer_manual_request_plan(
        "research NeuroFlow recent partnerships and clinical AI relevance",
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "business_research_analyst"
    assert plan.primary_target == "NeuroFlow"
    assert "NeuroFlow" in plan.objective


def test_manual_plan_routes_named_research_browser_diagnostics_to_chief_of_staff() -> None:
    plan = infer_manual_request_plan(
        (
            "Use backend browser diagnostics to check https://example.com for "
            "rendered-page, console, and network issues."
        ),
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "browser_diagnostics"
    assert plan.target_type == "url"
    assert plan.primary_target == "https://example.com"
    assert plan.requires_live_search is False


def test_manual_plan_preserves_browser_diagnostics_when_llm_candidate_misroutes() -> None:
    fallback = infer_manual_request_plan(
        "Use backend browser diagnostics to check https://example.com for console issues.",
        requested_agent="business research analyst",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target="https://example.com",
        target_type="company",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.intent == "browser_diagnostics"
    assert merged.target_agent == "chief_of_staff"
    assert merged.target_type == "url"
    assert any("Ignored planner override" in warning for warning in merged.planner_warnings)


def test_manual_plan_keeps_research_route_when_browser_diagnostics_are_optional() -> None:
    plan = infer_manual_request_plan(
        (
            "Use backend browser diagnostics only if needed. Research https://example.com "
            "and explain whether it is a real operating company."
        ),
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.target_type == "url"
    assert plan.primary_target == "https://example.com"


def test_manual_plan_preserves_outreach_approval_gate_in_orchestrator() -> None:
    plan = infer_manual_request_plan(
        "draft outreach to NeuroFlow about clinical AI evaluation",
        requested_agent="orchestrator",
    )
    result = route_request("draft outreach to NeuroFlow", manual_plan=plan)

    assert plan.requires_approved_context is True
    assert result.route == "clarification"
    assert result.approval_state == ApprovalState.PENDING
    assert result.refused is True
    assert result.send_enabled is False


def test_orchestrator_uses_manual_plan_before_broad_company_fallback() -> None:
    plan = infer_manual_request_plan(
        "Find 5 researchers in psychiatry AI with recent grants",
        requested_agent="orchestrator",
    )
    result = route_request(
        "Find 5 researchers in psychiatry AI with recent grants",
        manual_plan=plan,
    )

    assert result.route == "opportunity_scout"
    assert result.retrieval_hint is not None
    assert any("Manual request plan used" in note for note in result.audit_notes)


def test_manual_plan_extracts_gmail_scope_hints() -> None:
    plan = infer_manual_request_plan(
        "triage unread Gmail threads from the last 3 days and draft replies only for urgent items",
        requested_agent="gmail_triage",
    )

    assert plan.target_agent == "gmail_triage"
    assert plan.gmail_query == "is:unread newer_than:3d"
    assert plan.lookback_days == 3
    assert plan.draft_policy == "draft_only_for_urgent"


def test_manual_planner_configs_try_target_provider_then_openai(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "target_with_openai_fallback")

    configs = _planner_model_configs(requested_agent="gmail_triage")

    assert [config.provider for config in configs] == ["gemini", "openai"]


def test_cli_live_opportunity_scout_uses_script_retrieval_path(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"records": [], "send_enabled": False}),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "opportunity_scout",
                "--live-sdk",
                "--json",
                (
                    "Find 2 U.S.-relevant academic institutes. Use live SDK and live "
                    "search. No outreach."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "opportunity_scout"
    assert output["manual_request_plan"]["desired_count"] == 2
    assert "scripts/run_opportunity_scout.py" in captured["command"]
    assert "--live-search" in captured["command"]
    assert captured["command"][captured["command"].index("--max-results") + 1] == "2"
    assert captured["command"][captured["command"].index("--topic") + 1] == (
        "U.S.-relevant academic institutes"
    )


def test_cli_live_browser_diagnostics_named_research_reroutes_to_chief(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "output": {"summary": "Rendered page diagnostics completed."},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "business_research_analyst",
                "--live-sdk",
                "--json",
                (
                    "Use backend browser diagnostics to check https://example.com "
                    "for rendered-page, console, and network issues."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "chief_of_staff"
    assert output["manual_request_plan"]["intent"] == "browser_diagnostics"
    assert output["manual_request_plan"]["primary_target"] == "https://example.com"
    assert "scripts/run_chief_of_staff.py" in captured["command"]


def test_cli_live_business_research_url_target_passes_company_url(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "CompanyResearchFocusedBrief",
                    "output": {"company_name": "example.com", "source_ids_used": []},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "business_research_analyst",
                "--live-sdk",
                "--json",
                (
                    "Use backend browser diagnostics only if needed. Research https://example.com "
                    "and explain whether it is a real operating company."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "business_research_analyst"
    assert output["manual_request_plan"]["target_type"] == "url"
    assert captured["command"][captured["command"].index("--company") + 1] == "example.com"
    assert captured["command"][captured["command"].index("--company-url") + 1] == (
        "https://example.com"
    )


def test_cli_live_outreach_blocks_without_approved_context(capsys) -> None:
    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "outreach_composer",
                "--live-sdk",
                "--json",
                "draft outreach to NeuroFlow",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert output["requires_approved_context"] is True
    assert output["send_enabled"] is False
    assert "No default fixture was used" in output["message"]


def test_gmail_execution_plan_maps_recent_actionable_threads_to_priority_grouping() -> None:
    plan = infer_gmail_execution_plan(
        "review recent Gmail threads from the last 7 days related to Keystone opportunities "
        "or follow-ups. Identify the top 3 actionable threads, summarize each, and draft "
        "replies only where a reply is needed. Do not send."
    )

    assert plan.operation == "priority_grouping"
    assert plan.lookback_days == 7
    assert plan.live_read_required is True
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is True
    assert plan.gmail_query == "newer_than:7d"
    assert "gmail_priority_grouping_sdk" in plan.candidate_helpers


def test_outreach_execution_plan_keeps_drafts_gated_and_tracks_replies() -> None:
    plan = infer_outreach_execution_plan(
        "draft a concise follow-up email for a source-backed Keystone opportunity, and "
        "include a plan for how future replies should be tracked or summarized. Do not send."
    )

    assert plan.operation == "draft_follow_up"
    assert plan.approved_context_required is True
    assert plan.use_default_approved_fixture_for_backend_test is True
    assert plan.include_follow_up_schedule is True
    assert plan.include_reply_tracking_plan is True
    assert plan.side_effect_policy == "draft_only_never_send"


def test_cli_live_gmail_priority_grouping_uses_agent_execution_plan(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "GmailPriorityGroupingResult",
                    "output": {"buckets": {}},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "gmail_triage",
                "--live-sdk",
                "--json",
                (
                    "review recent Gmail threads from the last 7 days related to "
                    "Keystone opportunities"
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "gmail_triage"
    assert output["agent_execution_plan"]["operation"] == "priority_grouping"
    assert "--live-gmail" in captured["command"]
    assert "--priority-grouping" in captured["command"]
    assert captured["command"][captured["command"].index("--lookback-days") + 1] == "7"
    assert captured["command"][captured["command"].index("--gmail-query") + 1] == "newer_than:7d"


def test_cli_live_outreach_backend_fixture_uses_agent_execution_plan(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "OutreachDraft",
                    "output": {"company_name": "Curebase", "send_enabled": False},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "outreach_composer",
                "--live-sdk",
                "--json",
                (
                    "draft a concise follow-up email for a source-backed Keystone opportunity, "
                    "and include a plan for how future replies should be tracked or summarized. "
                    "Do not send."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "outreach_composer"
    assert output["agent_execution_plan"]["operation"] == "draft_follow_up"
    assert "--include-follow-up-schedule" in captured["command"]
    assert "--use-example-rag" in captured["command"]
    assert captured["command"][captured["command"].index("--approval-decision") + 1] == "pending"
