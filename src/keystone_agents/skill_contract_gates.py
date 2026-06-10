"""Deterministic checks for skill-backed WorkItem contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemRoute,
)

GateStatus = str


@dataclass(frozen=True)
class SkillGateCheck:
    """One deterministic contract check tied to one or more skills."""

    gate_id: str
    domain: str
    status: GateStatus
    summary: str
    hard_gate: bool
    skill_ids: tuple[str, ...]
    eval_labels: tuple[str, ...]
    output_fields: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "domain": self.domain,
            "status": self.status,
            "summary": self.summary,
            "hard_gate": self.hard_gate,
            "skill_ids": list(self.skill_ids),
            "eval_labels": list(self.eval_labels),
            "output_fields": list(self.output_fields),
            "evidence": self.evidence,
        }


def evaluate_work_item_skill_gates(
    result: WorkflowRunResult,
    *,
    request_text: str = "",
) -> tuple[SkillGateCheck, ...]:
    """Return route-specific deterministic checks for the completed WorkItem step."""

    if result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return (check_business_research_claim_gate(result.work_item, result=result),)
    if result.route == WorkItemRoute.OUTREACH_COMPOSER:
        return (check_outreach_approval_claim_gate(result.work_item, result=result),)
    if result.route == WorkItemRoute.GMAIL_TRIAGE:
        return (
            check_gmail_sensitive_message_gate(
                result.work_item,
                result=result,
                request_text=request_text,
            ),
        )
    if result.route == WorkItemRoute.CHIEF_OF_STAFF:
        return (check_chief_artifact_publish_gate(result.work_item, result=result),)
    return ()


def check_business_research_claim_gate(
    work_item: WorkItem,
    *,
    result: WorkflowRunResult | None = None,
) -> SkillGateCheck:
    """Ensure research artifacts carry visible source basis or block/limit clearly."""

    artifacts = _artifacts_for(work_item, result, "company_profile")
    source_ref_count = len(work_item.sources) + sum(
        len(_source_refs_from_artifact(artifact)) for artifact in artifacts
    )
    blocker_codes = _blocker_codes(work_item, result)
    if artifacts and source_ref_count > 0:
        status = "passed"
        summary = "Business research output has source-backed claim context."
    elif blocker_codes:
        status = "blocked"
        summary = "Business research stopped with a blocker instead of fabricating claims."
    else:
        status = "limited"
        summary = "Business research output lacks enough source refs for claim confidence."
    return SkillGateCheck(
        gate_id="business_research_claim_gate",
        domain="business_research",
        status=status,
        summary=summary,
        hard_gate=True,
        skill_ids=(
            "business_research_specialist_contracts",
            "evidence_attribution_and_claim_mapping",
            "unsupported_claim_and_gap_handling",
        ),
        eval_labels=(
            "business_research.claim_gate",
            "evidence_attribution_and_claim_mapping",
            "unsupported_claim_and_gap_handling",
        ),
        output_fields=("sources", "claims", "unsupported_claims", "missing_evidence"),
        evidence={
            "artifact_count": len(artifacts),
            "source_ref_count": source_ref_count,
            "blocker_codes": blocker_codes,
        },
    )


def check_outreach_approval_claim_gate(
    work_item: WorkItem,
    *,
    result: WorkflowRunResult | None = None,
) -> SkillGateCheck:
    """Ensure outreach remains draft-only, approval-gated, and source/context backed."""

    artifacts = _artifacts_for(work_item, result, "outreach_draft")
    unsafe_flags = _unsafe_side_effect_flags(artifacts)
    external_gate = _has_approval_gate(work_item, ApprovalScope.EXTERNAL_USE.value)
    source_basis = _has_source_basis(work_item)
    blocker_codes = _blocker_codes(work_item, result)
    if unsafe_flags:
        status = "blocked"
        summary = "Outreach attempted an unsafe side-effect flag."
    elif artifacts and external_gate and source_basis:
        status = "passed"
        summary = "Outreach draft is no-send, approval-gated, and source/context backed."
    elif blocker_codes:
        status = "blocked"
        summary = "Outreach stopped with a blocker instead of drafting from unsafe context."
    else:
        status = "limited"
        summary = "Outreach context is incomplete; draft should remain blocked or review-only."
    return SkillGateCheck(
        gate_id="outreach_approval_claim_gate",
        domain="outreach",
        status=status,
        summary=summary,
        hard_gate=True,
        skill_ids=(
            "outreach_composer_specialist_contracts",
            "context_permission_gating",
            "action_boundary_enforcement",
            "evidence_attribution_and_claim_mapping",
            "unsupported_claim_and_gap_handling",
        ),
        eval_labels=(
            "outreach.approval_claim_gate",
            "context_permission_gating",
            "action_boundary_enforcement",
            "evidence_attribution_and_claim_mapping",
        ),
        output_fields=(
            "approval_required",
            "approval_status",
            "source_ids_used",
            "unsupported_claims_flagged",
            "send_enabled",
        ),
        evidence={
            "artifact_count": len(artifacts),
            "external_approval_gate": external_gate,
            "source_basis": source_basis,
            "unsafe_flags": unsafe_flags,
            "blocker_codes": blocker_codes,
        },
    )


def check_gmail_sensitive_message_gate(
    work_item: WorkItem,
    *,
    result: WorkflowRunResult | None = None,
    request_text: str = "",
) -> SkillGateCheck:
    """Ensure sensitive Gmail messages are flagged or safely blocked."""

    artifacts = _artifacts_for(work_item, result, "gmail_triage_report")
    unsafe_flags = _unsafe_side_effect_flags(artifacts)
    risk_flags = [
        str(flag)
        for artifact in artifacts
        for flag in _list(artifact.metadata.get("risk_flags"))
        if str(flag).strip()
    ]
    sensitive_request = bool(_SENSITIVE_GMAIL_RE.search(request_text or work_item.request_text))
    blocker_codes = _blocker_codes(work_item, result)
    context_blocked = "gmail_context_required" in blocker_codes
    if unsafe_flags:
        status = "blocked"
        summary = "Gmail triage attempted an unsafe side-effect flag."
    elif sensitive_request and artifacts and not risk_flags:
        status = "limited"
        summary = "Sensitive Gmail context was triaged but risk flags were not visible."
    elif artifacts or context_blocked:
        status = "passed"
        summary = "Gmail triage preserved read-only/sensitive-message boundaries."
    else:
        status = "limited"
        summary = "Gmail triage lacks selected thread context or visible sensitive-message review."
    return SkillGateCheck(
        gate_id="gmail_sensitive_message_gate",
        domain="gmail",
        status=status,
        summary=summary,
        hard_gate=True,
        skill_ids=(
            "gmail_triage_specialist_contracts",
            "context_permission_gating",
            "action_boundary_enforcement",
        ),
        eval_labels=(
            "gmail.sensitive_message_gate",
            "context_permission_gating",
            "action_boundary_enforcement",
        ),
        output_fields=(
            "risk_flags",
            "triage_limitations",
            "draft_created",
            "labels_modified",
            "send_enabled",
        ),
        evidence={
            "artifact_count": len(artifacts),
            "risk_flags": risk_flags,
            "sensitive_request": sensitive_request,
            "unsafe_flags": unsafe_flags,
            "blocker_codes": blocker_codes,
        },
    )


def check_chief_artifact_publish_gate(
    work_item: WorkItem,
    *,
    result: WorkflowRunResult | None = None,
) -> SkillGateCheck:
    """Ensure Chief of Staff artifacts do not imply publish permission."""

    artifacts = _artifacts_for(work_item, result, "chief_of_staff_plan")
    publish_flags = [
        flag for flag in _unsafe_side_effect_flags(artifacts) if flag.endswith("=true")
    ]
    needs_approval = bool(
        result is not None
        and result.next_action is not None
        and result.next_action.requires_approval
    )
    has_gate = bool(work_item.approval_gates)
    blocker_codes = _blocker_codes(work_item, result)
    if publish_flags and not (needs_approval or has_gate):
        status = "blocked"
        summary = "Chief artifact indicates publish/write permission without approval state."
    elif artifacts:
        status = "passed"
        summary = "Chief artifact keeps publish/write scope explicit and reviewable."
    elif blocker_codes:
        status = "blocked"
        summary = "Chief workflow stopped with a blocker instead of publishing unsafely."
    else:
        status = "limited"
        summary = "Chief workflow did not produce a reviewable artifact or blocker."
    return SkillGateCheck(
        gate_id="chief_artifact_publish_gate",
        domain="chief_of_staff",
        status=status,
        summary=summary,
        hard_gate=True,
        skill_ids=(
            "chief_of_staff_specialist_contracts",
            "workspace_artifact_governance",
            "action_boundary_enforcement",
        ),
        eval_labels=(
            "chief.artifact_publish_gate",
            "workspace_artifact_governance",
            "action_boundary_enforcement",
        ),
        output_fields=(
            "artifact_metadata",
            "approval_state",
            "audience",
            "source_scope",
            "next_safe_action",
        ),
        evidence={
            "artifact_count": len(artifacts),
            "publish_flags": publish_flags,
            "next_action_requires_approval": needs_approval,
            "approval_gate_count": len(work_item.approval_gates),
            "blocker_codes": blocker_codes,
        },
    )


_SENSITIVE_GMAIL_RE = re.compile(
    r"\b(?:phi|patient|medical|diagnosis|payment|invoice|wire|bank|credential|password|"
    r"legal|contract|attachment|link|security|spoof|phish|urgent)\b",
    flags=re.I,
)


def _artifacts_for(
    work_item: WorkItem,
    result: WorkflowRunResult | None,
    artifact_type: str,
) -> list[WorkItemArtifactRef]:
    refs = list(result.artifact_refs if result is not None else ())
    refs.extend(work_item.artifact_refs)
    by_key: dict[tuple[str, str], WorkItemArtifactRef] = {}
    for ref in refs:
        if ref.artifact_type == artifact_type:
            by_key[(ref.artifact_type, ref.artifact_id)] = ref
    return list(by_key.values())


def _source_refs_from_artifact(artifact: WorkItemArtifactRef) -> list[Any]:
    refs = artifact.metadata.get("source_refs")
    if isinstance(refs, list):
        return refs
    source_count = artifact.metadata.get("source_count")
    if isinstance(source_count, int) and source_count > 0:
        return [None] * source_count
    return []


def _blocker_codes(work_item: WorkItem, result: WorkflowRunResult | None) -> list[str]:
    blockers = list(work_item.blockers)
    if result is not None:
        blockers.extend(result.blockers)
    return sorted(
        {str(blocker.code) for blocker in blockers if not getattr(blocker, "resolved", False)}
    )


def _unsafe_side_effect_flags(artifacts: list[WorkItemArtifactRef]) -> list[str]:
    unsafe: list[str] = []
    for artifact in artifacts:
        metadata = artifact.metadata
        for key in (
            "send_enabled",
            "email_sent",
            "external_write_performed",
            "gmail_draft_created",
            "labels_modified",
            "slack_post_allowed",
        ):
            if metadata.get(key) is True:
                unsafe.append(f"{artifact.artifact_type}.{key}=true")
    return unsafe


def _has_approval_gate(work_item: WorkItem, scope: str) -> bool:
    return any(
        gate.scope == scope
        and gate.state
        in {
            ApprovalState.PENDING.value,
            ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
            ApprovalState.APPROVED_FOR_SEND.value,
        }
        for gate in work_item.approval_gates
    )


def _has_source_basis(work_item: WorkItem) -> bool:
    if work_item.sources:
        return True
    return any(_source_refs_from_artifact(artifact) for artifact in work_item.artifact_refs)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]
