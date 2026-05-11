from __future__ import annotations

from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)


def test_work_item_schema_defaults_and_artifact_id_coercion() -> None:
    item = WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Research Lindus")
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id=12,
        source_agent="business_research_analyst",
    )

    updated = item.model_copy(
        update={
            "status": WorkItemStatus.IN_PROGRESS,
            "current_route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            "artifact_refs": [artifact],
            "next_action": WorkItemNextAction(
                action="review_company_profile",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
            ),
        }
    )

    assert item.id.startswith("wi_")
    assert updated.artifact_refs[0].artifact_id == "12"
    assert updated.current_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert updated.next_action is not None
    assert updated.next_action.agent == WorkItemRoute.OPPORTUNITY_SCOUT
