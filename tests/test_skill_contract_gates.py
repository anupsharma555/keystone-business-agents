from __future__ import annotations

from keystone_agents.schemas.approval import ApprovalScope
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.skill_contract_gates import (
    check_business_research_claim_gate,
    check_chief_artifact_publish_gate,
    check_gmail_sensitive_message_gate,
    check_outreach_approval_claim_gate,
)


def _item(route: WorkItemRoute) -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Test WorkItem",
        current_route=route,
    )


def test_business_research_claim_gate_degrades_missing_sources_to_limitation() -> None:
    item = _item(WorkItemRoute.BUSINESS_RESEARCH_ANALYST).model_copy(
        update={
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="company_profile",
                    artifact_id="1",
                    source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    metadata={},
                )
            ]
        }
    )

    gate = check_business_research_claim_gate(item)

    assert gate.status == "limited"
    assert gate.gate_id == "business_research_claim_gate"
    assert "evidence_attribution_and_claim_mapping" in gate.eval_labels


def test_outreach_gate_blocks_unsafe_send_flag_instead_of_crashing() -> None:
    item = _item(WorkItemRoute.OUTREACH_COMPOSER).model_copy(
        update={
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="outreach_draft",
                    artifact_id="1",
                    source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
                    metadata={"send_enabled": True},
                )
            ],
            "approval_gates": [
                WorkItemApprovalGate(scope=ApprovalScope.EXTERNAL_USE.value, state="pending")
            ],
        }
    )

    gate = check_outreach_approval_claim_gate(item)

    assert gate.status == "blocked"
    assert gate.evidence["unsafe_flags"] == ["outreach_draft.send_enabled=true"]
    assert "action_boundary_enforcement" in gate.eval_labels


def test_gmail_gate_marks_sensitive_message_without_visible_risk_flags_limited() -> None:
    item = _item(WorkItemRoute.GMAIL_TRIAGE).model_copy(
        update={
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="gmail_triage_report",
                    artifact_id="1",
                    source_agent=WorkItemRoute.GMAIL_TRIAGE.value,
                    metadata={"send_enabled": False},
                )
            ]
        }
    )
    result = WorkflowRunResult(
        work_item=item,
        route=WorkItemRoute.GMAIL_TRIAGE,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=list(item.artifact_refs),
    )

    gate = check_gmail_sensitive_message_gate(
        item,
        result=result,
        request_text="Email mentions a payment attachment and credential reset link.",
    )

    assert gate.status == "limited"
    assert gate.evidence["sensitive_request"] is True
    assert "gmail.sensitive_message_gate" in gate.eval_labels


def test_chief_publish_gate_blocks_publish_flag_without_approval_state() -> None:
    item = _item(WorkItemRoute.CHIEF_OF_STAFF).model_copy(
        update={
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="chief_of_staff_plan",
                    artifact_id="1",
                    source_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
                    metadata={"slack_post_allowed": True},
                )
            ],
            "blockers": [
                WorkItemBlocker(code="prior_context_missing", message="Need current state.")
            ],
        }
    )
    result = WorkflowRunResult(
        work_item=item,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.BLOCKED,
        advanced=False,
        artifact_refs=list(item.artifact_refs),
    )

    gate = check_chief_artifact_publish_gate(item, result=result)

    assert gate.status == "blocked"
    assert gate.evidence["publish_flags"] == ["chief_of_staff_plan.slack_post_allowed=true"]
    assert "workspace_artifact_governance" in gate.eval_labels
