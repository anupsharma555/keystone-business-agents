"""Accepted Research retries resolve execution failures without clearing other gates."""

from __future__ import annotations

import pytest

from keystone_agents import workflow_runner
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.storage import SQLiteStore

FAILURE = "business_research_analyst_execution_failed"
SOURCE_ID = "synthetic-source"
SOURCE_URL = "https://example.org/synthetic-study"


def blocked_item():
    return WorkItem(
        id="wi_synthetic_research_recovery", kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.BLOCKED, current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        title="Synthetic evidence review", request_text="Review only the supplied evidence.",
        target=WorkItemTarget(name="Synthetic study", object_type="topic"),
        blockers=[WorkItemBlocker(code=FAILURE, message="An earlier model attempt failed.")],
        sources=[WorkItemSourceRef(
            source_id=SOURCE_ID, url=SOURCE_URL, title="Synthetic study",
            source_type="public_article", provider="operator_supplied",
            extraction_status="supplied_material",
            evidence_excerpt="A small observational study does not establish causal effects.",
        )],
    )


def accepted_brief():
    return ResearchBrief(
        target_name="Synthetic study", target_type="topic", summary="Evidence remains preliminary.",
        facts=[{"text": "The study was observational.", "source_ids": [SOURCE_ID]}],
        source_ids_used=[SOURCE_ID], sources=[{
            "source_id": SOURCE_ID, "title": "Synthetic study", "url": SOURCE_URL,
        }],
        decision={
            "decision_stage": "research_source_selection", "selected_candidate_ids": [SOURCE_ID],
            "needs_more_context": False, "reasoning": "Use the supplied observational evidence.",
            "candidate_assessments": [{
                "candidate_id": SOURCE_ID, "disposition": "selected",
                "rationale": "The provided source supports this limited finding.",
            }],
        },
    )


def run_supplied(item, tmp_path, monkeypatch, *, brief=None, error=None, manual_plan=None):
    database_url = f"sqlite:///{tmp_path / 'recovery.db'}"
    store = SQLiteStore(database_url)
    store.save_work_item(item)

    def sdk_result(*args, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["live"] is True
        if error is not None:
            raise error
        return TypedAgentRunResult(
            agent_name="business_research_analyst", output=brief or accepted_brief(),
            raw_result=None,
        )

    monkeypatch.setattr(
        workflow_runner, "run_business_research_analyst_research_brief_sdk", sdk_result,
    )
    request = WorkflowRunRequest(
        request_text=item.request_text, work_item_id=item.id,
        database_url=database_url, live_sdk=True, live_search=False,
        sdk_session_enabled=False,
        manual_request_plan=manual_plan,
    )
    result = workflow_runner._supplied_business_research_sdk_result(
        item, request=request, store=store,
    )
    result = workflow_runner.finalize_prepared_work_item_step(
        workflow_runner.PreparedWorkItemStep(
            request=request, work_item=item, route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            input_text=item.request_text, context_pack={},
        ),
        result, synthesize_user_response=False,
    )
    return result, store


def test_accepted_sdk_artifact_resolves_only_prior_research_execution_failure(
    tmp_path, monkeypatch,
):
    item = blocked_item()
    result, store = run_supplied(item, tmp_path, monkeypatch)
    assert result.advanced
    assert result.work_item.id == item.id
    assert result.status != WorkItemStatus.BLOCKED
    assert len(result.artifact_refs) == 1
    assert result.artifact_refs[0].artifact_type == "research_brief"
    resolved = next(blocker for blocker in result.work_item.blockers if blocker.code == FAILURE)
    assert resolved.resolved and resolved.resolved_at
    assert resolved.message == item.blockers[0].message
    assert not item.blockers[0].resolved
    saved = store.get_work_item(item.id)
    assert saved.blockers[0].resolved
    assert saved.artifact_refs[0].artifact_id == result.artifact_refs[0].artifact_id


@pytest.mark.parametrize("code", [
    "source_sufficiency_required", "outreach_composer_execution_failed",
])
def test_success_keeps_unrelated_source_and_other_route_blockers(tmp_path, monkeypatch, code):
    item = blocked_item()
    item.blockers.append(WorkItemBlocker(code=code, message="An independent condition remains."))
    result, store = run_supplied(item, tmp_path, monkeypatch)
    assert result.advanced and result.artifact_refs
    assert result.status == WorkItemStatus.BLOCKED
    states = {blocker.code: blocker.resolved for blocker in result.work_item.blockers}
    assert states == {FAILURE: True, code: False}
    assert any(not blocker.resolved and blocker.code == code
               for blocker in store.get_work_item(item.id).blockers)


def test_success_keeps_pending_approval_gate(tmp_path, monkeypatch):
    item = blocked_item()
    item.approval_gates.append(WorkItemApprovalGate(
        scope="external_use", state="pending", required=True,
    ))
    result, store = run_supplied(item, tmp_path, monkeypatch)
    assert result.advanced
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert result.work_item.approval_gates == item.approval_gates
    assert store.get_work_item(item.id).approval_gates[0].state == "pending"


@pytest.mark.parametrize("outcome", ["exception", "needs_context"])
def test_failed_or_incomplete_sdk_attempt_does_not_resolve_execution_blocker(
    tmp_path, monkeypatch, outcome,
):
    brief = ResearchBrief(target_name="Synthetic study", summary="More evidence is required.")
    result, store = run_supplied(
        blocked_item(), tmp_path, monkeypatch,
        brief=brief if outcome == "needs_context" else None,
        error=RuntimeError("Synthetic failed execution") if outcome == "exception" else None,
    )
    assert not result.advanced
    assert not result.artifact_refs
    assert result.status == WorkItemStatus.BLOCKED
    saved = store.get_work_item(result.work_item.id)
    assert any(blocker.code == FAILURE and not blocker.resolved for blocker in saved.blockers)
    assert not saved.artifact_refs


@pytest.mark.parametrize("current_bound_plan", [True, False])
def test_only_accepted_current_source_binding_resolves_prior_decision_failure(
    tmp_path, monkeypatch, current_bound_plan,
):
    item = blocked_item()
    item.target.url = SOURCE_URL
    item.target.metadata["selected_source_resume_binding"] = {
        "schema": "keystone.selected_source_resume_binding.v1",
        "work_item_id": item.id, "source_id": SOURCE_ID, "source_url": SOURCE_URL,
    }
    invalid = "business_research_analyst_agent_decision_invalid"
    item.blockers.extend([
        WorkItemBlocker(code=invalid, message="The prior tool contract was invalid."),
        WorkItemBlocker(code="source_sufficiency_required", message="An unrelated gate remains."),
    ])
    result, store = run_supplied(
        item, tmp_path, monkeypatch,
        manual_plan={"source": "canonical:work_item_selected_source_resume"}
        if current_bound_plan else None,
    )
    states = {blocker.code: blocker for blocker in store.get_work_item(item.id).blockers}
    assert states[FAILURE].resolved and states[FAILURE].resolved_at
    assert states[invalid].resolved == current_bound_plan
    assert bool(states[invalid].resolved_at) == current_bound_plan
    assert not states["source_sufficiency_required"].resolved
    assert result.artifact_refs and result.status == WorkItemStatus.BLOCKED
