from __future__ import annotations

import re
from pathlib import Path

from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.langgraph_workflow import should_use_langgraph_for_work_item
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.schemas.work_item import WorkflowRunRequest

JOBS_DOC = Path("docs/HUMAN_AGENT_EXECUTION_JOBS.md")


def _job_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in JOBS_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| HJ-"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        prompt_match = re.fullmatch(r"`(.+)`", cells[2])
        assert prompt_match is not None
        rows.append(
            {
                "id": cells[0],
                "tier": cells[1],
                "prompt": prompt_match.group(1),
                "owner": cells[3],
                "capability": cells[4],
                "proof": cells[5],
            }
        )
    return rows


def test_human_job_catalogue_covers_every_agent_family_and_complexity_tier() -> None:
    rows = _job_rows()

    assert [row["id"] for row in rows] == [f"HJ-{index:03d}" for index in range(1, 33)]
    assert {row["tier"] for row in rows} == {"simple", "intermediate", "advanced"}
    assert {row["owner"] for row in rows} == {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
        "chief_of_staff",
    }
    for row in rows:
        prompt = row["prompt"].lower()
        assert row["capability"]
        assert row["proof"]
        assert "test case" not in prompt
        assert "validation" not in prompt
        assert "promptfoo" not in prompt
        assert "--live" not in prompt


def test_human_jobs_route_from_natural_language_to_the_expected_owner() -> None:
    for row in _job_rows():
        if row["id"] == "HJ-026":
            calendar_plan = infer_calendar_action_plan(row["prompt"])
            assert calendar_plan is not None
            assert calendar_plan.complete is True
            assert calendar_plan.operation == "create"
            continue
        plan = infer_manual_request_plan(row["prompt"], requested_agent="orchestrator")

        assert plan.target_agent == row["owner"], row["id"]
        assert plan.intent != "clarification", row["id"]


def test_human_jobs_preserve_key_scope_and_workflow_constraints() -> None:
    rows = {row["id"]: row for row in _job_rows()}
    daily = infer_manual_request_plan(rows["HJ-001"]["prompt"], requested_agent="orchestrator")
    draft = infer_manual_request_plan(rows["HJ-002"]["prompt"], requested_agent="orchestrator")
    preprints = infer_manual_request_plan(
        rows["HJ-016"]["prompt"], requested_agent="orchestrator"
    )
    workflow = infer_manual_request_plan(
        rows["HJ-019"]["prompt"], requested_agent="orchestrator"
    )
    gmail_update = infer_gmail_execution_plan(rows["HJ-003"]["prompt"])
    gmail_create = infer_gmail_execution_plan(
        "Find the latest email from Example Health and create a Gmail draft reply. Do not send."
    )
    gmail_update_by_reference = infer_gmail_execution_plan(
        "Update the existing Gmail draft with subject 'Project follow-up' for "
        "reviewer@example.com to be shorter and warmer. Do not send."
    )
    gmail_style = infer_gmail_execution_plan(rows["HJ-024"]["prompt"])

    assert daily.lookback_days == 1
    assert daily.gmail_query.startswith("after:")
    assert draft.gmail_query == '"Example Health"'
    assert draft.draft_policy == "draft_only_when_reply_needed"
    assert preprints.desired_count == 3
    assert workflow.intent == "opportunity_to_outreach_loop"
    assert workflow.side_effect_policy == "draft_or_read_only"
    assert gmail_update.operation == "update_draft"
    assert gmail_update.source_label == "DRAFT"
    assert gmail_update.candidate_helpers == ["gmail_draft_read", "gmail_draft_update"]
    assert gmail_update.create_gmail_drafts is True
    assert gmail_update.side_effect_policy == "scoped_gmail_draft_write_no_send"
    assert gmail_update.planner_warnings
    assert gmail_update_by_reference.operation == "update_draft"
    assert gmail_update_by_reference.draft_subject_hint == "Project follow-up"
    assert gmail_update_by_reference.draft_recipient_hint == "reviewer@example.com"
    assert gmail_update_by_reference.planner_warnings == []
    assert gmail_create.operation == "draft_reply"
    assert gmail_create.create_gmail_drafts is True
    assert "gmail_verified_reply_draft_create" in gmail_create.candidate_helpers
    assert gmail_create.side_effect_policy == "scoped_gmail_draft_write_no_send"
    assert gmail_style.operation == "style_profile"
    assert gmail_style.source_label == "SENT"
    assert gmail_style.max_messages == 5
    assert "aggregate_email_style_profile_build" in gmail_style.candidate_helpers
    assert gmail_style.planner_warnings


def test_advanced_human_jobs_select_the_graph_backend() -> None:
    advanced = [row for row in _job_rows() if row["tier"] == "advanced"]

    for row in advanced:
        plan = infer_manual_request_plan(row["prompt"], requested_agent="orchestrator")
        request = WorkflowRunRequest(
            request_text=row["prompt"],
            manual_request_plan=plan.model_dump(mode="json"),
        )

        assert should_use_langgraph_for_work_item(request, manager_loop=True), row["id"]


def test_representative_zotero_proof_keeps_provider_identity_internal() -> None:
    zotero = next(row for row in _job_rows() if row["id"] == "HJ-022")
    proof = zotero["proof"].lower()
    assert "no provider id was required" in proof
    assert "internally retained key" in proof
    assert "version-aware verification" in proof


def test_human_job_contract_requires_reasoning_execution_verification_and_cleanup() -> None:
    text = " ".join(JOBS_DOC.read_text(encoding="utf-8").split())

    for phrase in (
        "reasoning quality against the user's actual question",
        "typed tool calls and structured output type",
        "read-back/modification verification and cleanup status",
        "no-send/no-post/no-publish confirmation",
        "Deterministic extraction owns explicit dates/windows",
        "configured planning model owns ambiguous entity resolution",
        "No model may override no-send",
        "Use the non-graph path when one durable agent owns the job",
        "Use LangGraph when the validated plan requires multiple agent stages",
        "Python selects the backend from validated plan structure",
        "Framework execution and live tool execution are separate statuses",
        "Agent tool execution validated",
        "Live natural-language agent lifecycle completion remains to be proven",
        "mandatory `KBA_TEST_RECORD` marker",
        "15 newest usable root runs",
        "workflow completion was often reported without completing the human job",
        "Direct success cannot be assumed to prove graph handoffs",
    ):
        assert phrase in text
