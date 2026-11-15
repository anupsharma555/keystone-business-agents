"""Graph results distinguish saved evidence from an unverified specialist step."""

from keystone_agents import workflow_runner
from keystone_agents.presentation.public_result import (
    attach_execution_public_result,
    build_work_item_result_payload,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)


def test_invalid_specialist_decision_has_clear_backend_failure_sections() -> None:
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic source review",
        request_text="Summarize the selected source and retain the task.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    result = workflow_runner._blocked_work_item_specialist_decision(
        item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        store=None,
        reason="The tool stage failed with ToolExecutionContractError.",
    )

    for heading in ("*Finding:*", "*Evidence limit:*", "*Task status:*", "*Next action:*"):
        assert heading in result.human_summary
    assert "no new finding" in result.human_summary.lower()
    assert "missing context" not in result.human_summary.lower()
    assert "provide the missing" not in result.human_summary.lower()
    assert "ToolExecutionContractError" not in result.human_summary
    assert item.id not in result.human_summary

    payload = build_work_item_result_payload(result, user_facing_result_verified=False)
    public = attach_execution_public_result(payload)
    assert public.status == "blocked"
    assert public.title == "Business Agents Review Needed"
    assert public.completion_confirmed is False


def test_actual_missing_source_context_still_requests_input() -> None:
    from keystone_agents.schemas.work_item import WorkItemBlocker

    summary = workflow_runner._blocked_human_summary(
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        (
            WorkItemBlocker(
                code="source_sufficiency_required", message="Selected source is missing."
            ),
        ),
    )
    assert "needs one more input" in summary
    assert "Selected source is missing" in summary


def test_graph_source_brief_keeps_findings_and_adds_task_continuation() -> None:
    artifact = WorkItemArtifactRef(artifact_type="research_brief", artifact_id="synthetic-brief")
    next_action = WorkItemNextAction(
        action="review_research_brief", agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    )
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic source review",
        request_text="Summarize the selected source and retain the task.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        artifact_refs=[artifact],
        next_action=next_action,
    )
    summary = (
        "**Findings:**\nThe selected source reports a measured result.\n\n"
        "**Limitations:**\nThe source does not establish generalizability.\n\n"
        "**Sources:**\n[Selected study](https://example.org/study)"
    )
    result = WorkflowRunResult(
        work_item=item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        next_action=next_action,
        human_summary=summary,
    )

    payload = build_work_item_result_payload(
        result,
        user_facing_result_verified=True,
        graph_metadata={"runtime": "langgraph"},
    )
    assert payload["human_summary"].startswith(summary)
    assert "*Task status:*" in payload["human_summary"]
    assert "*Next action:*" in payload["human_summary"]
    assert "natural-language follow-up" in payload["human_summary"].lower()
    assert item.id not in payload["human_summary"]

    without_graph = build_work_item_result_payload(
        result, user_facing_result_verified=True,
    )
    without_attached_brief = build_work_item_result_payload(
        result.model_copy(update={"artifact_refs": []}),
        user_facing_result_verified=True,
        graph_metadata={"runtime": "langgraph"},
    )
    assert without_graph["human_summary"] == summary
    assert without_attached_brief["human_summary"] == summary


def test_exact_output_request_does_not_gain_task_footer() -> None:
    artifact = WorkItemArtifactRef(artifact_type="research_brief", artifact_id="synthetic-brief")
    next_action = WorkItemNextAction(action="review_research_brief")
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic exact answer",
        request_text="Answer in exactly five words.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        artifact_refs=[artifact],
        next_action=next_action,
    )
    result = WorkflowRunResult(
        work_item=item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        next_action=next_action,
        human_summary="One two three four five.",
        manual_request_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "research_brief",
            "ask_shape": {"word_count": 5, "word_count_mode": "exact"},
        },
    )
    payload = build_work_item_result_payload(
        result,
        user_facing_result_verified=True,
        graph_metadata={"runtime": "langgraph"},
    )
    assert payload["human_summary"] == "One two three four five."
