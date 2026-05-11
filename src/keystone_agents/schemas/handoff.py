"""Typed cross-agent handoff contracts for Keystone workflows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.opportunity import OpportunityRecord as ScoutOpportunityRecord
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft

HandoffContractName = Literal[
    "opportunity_scout_to_business_research_analyst",
    "business_research_analyst_to_outreach_composer",
    "outreach_composer_to_orchestrator",
    "orchestrator_to_approval_review",
]
HandoffIssueSeverity = Literal["error", "warning"]


def _is_empty_required_value(value: Any) -> bool:
    return value is None or value == "" or value == () or value == []


class HandoffIssue(BaseModel):
    """One validation issue for a cross-agent handoff."""

    severity: HandoffIssueSeverity = "error"
    field: str
    message: str


class HandoffContractResult(BaseModel):
    """Validation result and compact review metadata for one agent handoff."""

    contract_name: HandoffContractName
    from_agent: str
    to_agent: str
    required_fields: list[str] = Field(default_factory=list)
    allowed_optional_fields: list[str] = Field(default_factory=list)
    source_ids_required: list[str] = Field(default_factory=list)
    source_ids_present: list[str] = Field(default_factory=list)
    unsupported_claims_required: list[str] = Field(default_factory=list)
    unsupported_claims_present: list[str] = Field(default_factory=list)
    missing_evidence_required: list[str] = Field(default_factory=list)
    missing_evidence_present: list[str] = Field(default_factory=list)
    valid: bool = True
    issues: list[HandoffIssue] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)

    def to_approval_metadata(self) -> dict[str, Any]:
        """Return a compact, body-free metadata payload for review queues."""

        return {
            "contract_name": self.contract_name,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "valid": self.valid,
            "source_ids": {
                "required": self.source_ids_required,
                "present": self.source_ids_present,
                "missing": sorted(set(self.source_ids_required) - set(self.source_ids_present)),
            },
            "unsupported_claims_flagged": self.unsupported_claims_present,
            "missing_evidence_reasons": self.missing_evidence_present,
            "issue_count": len(self.issues),
            "issues": [
                {
                    "severity": issue.severity,
                    "field": issue.field,
                    "message": issue.message,
                }
                for issue in self.issues
            ],
        }


class HandoffContractError(RuntimeError):
    """Raised when a required Keystone handoff contract is invalid."""


def _unique(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _source_ids_from_sources(sources: Sequence[Any]) -> list[str]:
    return _unique(getattr(source, "source_id", "") for source in sources)


def _source_ids_from_claims(claims: Sequence[Any]) -> list[str]:
    return _unique(getattr(claim, "source_id", "") for claim in claims)


def _source_ids_from_research_data_points(values: Sequence[Any]) -> list[str]:
    ids: list[str] = []
    for value in values:
        ids.extend(str(source_id) for source_id in getattr(value, "source_ids", []) or [])
    return _unique(ids)


def _missing_reasons_from_research_data_points(values: Sequence[Any]) -> list[str]:
    reasons: list[str] = []
    for value in values:
        if getattr(value, "completed", False):
            continue
        label = getattr(value, "label", "") or getattr(value, "key", "research data point")
        reason = getattr(value, "missing_reason", "") or "No source-backed value available."
        reasons.append(f"{label}: {reason}")
    return _unique(reasons)


def _optional_field_values_present(value: Any, fields: Sequence[str]) -> list[str]:
    present: list[str] = []
    for field in fields:
        if hasattr(value, field):
            present.append(field)
    return present


def _required_field_issues(
    value: Any,
    *,
    fields: Sequence[str],
    non_empty_fields: Sequence[str],
) -> list[HandoffIssue]:
    issues: list[HandoffIssue] = []
    for field in fields:
        if not hasattr(value, field):
            issues.append(
                HandoffIssue(field=field, message=f"Required handoff field {field} is absent.")
            )
            continue
        field_value = getattr(value, field)
        if field in non_empty_fields and _is_empty_required_value(field_value):
            issues.append(
                HandoffIssue(field=field, message=f"Required handoff field {field} is empty.")
            )
    return issues


def _unknown_claim_source_issues(
    *,
    claims: Sequence[Any],
    known_source_ids: Sequence[str],
    field: str,
) -> list[HandoffIssue]:
    known = set(known_source_ids)
    unknown_ids = sorted(
        {
            getattr(claim, "source_id", "")
            for claim in claims
            if getattr(claim, "source_id", "") and getattr(claim, "source_id", "") not in known
        }
    )
    if not unknown_ids:
        return []
    return [
        HandoffIssue(
            field=field,
            message=f"Claims cite source IDs not present in the handoff: {', '.join(unknown_ids)}.",
        )
    ]


def _build_result(
    *,
    contract_name: HandoffContractName,
    from_agent: str,
    to_agent: str,
    required_fields: Sequence[str],
    allowed_optional_fields: Sequence[str],
    source_ids_required: Sequence[str],
    source_ids_present: Sequence[str],
    unsupported_claims_required: Sequence[str],
    unsupported_claims_present: Sequence[str],
    missing_evidence_required: Sequence[str],
    missing_evidence_present: Sequence[str],
    issues: Sequence[HandoffIssue],
    audit_notes: Sequence[str],
) -> HandoffContractResult:
    required_sources = _unique(source_ids_required)
    present_sources = _unique(source_ids_present)
    source_issues = [
        HandoffIssue(
            field="source_ids_present",
            message=f"Required source ID {source_id} was not preserved in the handoff.",
        )
        for source_id in sorted(set(required_sources) - set(present_sources))
    ]
    all_issues = [*issues, *source_issues]
    return HandoffContractResult(
        contract_name=contract_name,
        from_agent=from_agent,
        to_agent=to_agent,
        required_fields=list(required_fields),
        allowed_optional_fields=list(allowed_optional_fields),
        source_ids_required=required_sources,
        source_ids_present=present_sources,
        unsupported_claims_required=_unique(unsupported_claims_required),
        unsupported_claims_present=_unique(unsupported_claims_present),
        missing_evidence_required=_unique(missing_evidence_required),
        missing_evidence_present=_unique(missing_evidence_present),
        valid=not any(issue.severity == "error" for issue in all_issues),
        issues=all_issues,
        audit_notes=_unique(audit_notes),
    )


def validate_opportunity_to_business_research_analyst(
    record: ScoutOpportunityRecord,
) -> HandoffContractResult:
    """Validate Scout evidence before Business Research Analyst consumes it."""

    required_fields = [
        "company_name",
        "opportunity_type",
        "priority_score",
        "why_now_signal",
        "recommended_next_step",
        "sources",
        "claims",
        "missing_evidence",
        "unsupported_claims_flagged",
        "approval_required_before_outreach",
    ]
    optional_fields = [
        "role_title",
        "role_location",
        "role_remote",
        "role_country",
        "role_fit_reason",
        "source_quality_summary",
        "source_signals",
        "source_bundles",
        "score_breakdown",
        "score_rationale",
        "contradictions",
        "weak_evidence_reasons",
        "handoff_reason",
        "business_research_analyst_handoff_recommendation",
        "research_needed",
        "disqualification_reasons",
    ]
    source_ids = _source_ids_from_sources(record.sources)
    issues = [
        *_required_field_issues(
            record,
            fields=required_fields,
            non_empty_fields=[
                "company_name",
                "opportunity_type",
                "why_now_signal",
                "recommended_next_step",
                "sources",
                "claims",
            ],
        ),
        *_unknown_claim_source_issues(
            claims=record.claims,
            known_source_ids=source_ids,
            field="claims",
        ),
    ]
    missing = _unique(
        [
            *record.missing_evidence,
            *record.weak_evidence_reasons,
            *record.contradictions,
            *record.research_needed,
        ]
    )
    return _build_result(
        contract_name="opportunity_scout_to_business_research_analyst",
        from_agent="opportunity_scout",
        to_agent="business_research_analyst",
        required_fields=required_fields,
        allowed_optional_fields=_optional_field_values_present(record, optional_fields),
        source_ids_required=source_ids,
        source_ids_present=[*source_ids, *_source_ids_from_claims(record.claims)],
        unsupported_claims_required=record.unsupported_claims_flagged,
        unsupported_claims_present=record.unsupported_claims_flagged,
        missing_evidence_required=missing,
        missing_evidence_present=missing,
        issues=issues,
        audit_notes=[
            "Opportunity evidence, source IDs, unsupported claim flags, and missing "
            "evidence reasons are available for business research."
        ],
    )


def validate_business_research_analyst_to_outreach_composer(
    profile: CompanyProfile,
    *,
    opportunity_record: ScoutOpportunityRecord | None = None,
) -> HandoffContractResult:
    """Validate Business Research Analyst output before draft-only outreach composition."""

    required_fields = [
        "name",
        "consulting_fit_score",
        "confidence_score",
        "sources",
        "claims",
        "research_data_points",
        "missing_evidence",
        "missing_information",
        "unsupported_claims_flagged",
    ]
    optional_fields = [
        "website",
        "lead_name",
        "linkedin_url",
        "description",
        "fit_summary",
        "source_quality_summary",
        "research_completeness",
        "features",
        "evidence",
        "contradictions",
        "risks",
        "opportunity_record",
    ]
    profile_source_ids = _source_ids_from_sources(profile.sources)
    opportunity_source_ids = (
        _source_ids_from_sources(opportunity_record.sources) if opportunity_record else []
    )
    source_ids = _unique([*profile_source_ids, *opportunity_source_ids])
    issues = [
        *_required_field_issues(
            profile,
            fields=required_fields,
            non_empty_fields=["name", "sources", "claims"],
        ),
        *_unknown_claim_source_issues(
            claims=profile.claims,
            known_source_ids=profile_source_ids,
            field="claims",
        ),
    ]
    if not (profile.fit_summary.strip() or profile.description.strip()):
        issues.append(
            HandoffIssue(
                field="fit_summary",
                message=(
                    "Business Research Analyst handoff needs a human-readable fit summary "
                    "or description."
                ),
            )
        )
    if opportunity_record is not None:
        issues.extend(
            _unknown_claim_source_issues(
                claims=opportunity_record.claims,
                known_source_ids=opportunity_source_ids,
                field="opportunity_record.claims",
            )
        )
    missing = _unique(
        [
            *profile.missing_evidence,
            *profile.missing_information,
            *profile.contradictions,
            *_missing_reasons_from_research_data_points(profile.research_data_points),
            *(
                [
                    *opportunity_record.missing_evidence,
                    *opportunity_record.weak_evidence_reasons,
                    *opportunity_record.contradictions,
                ]
                if opportunity_record is not None
                else []
            ),
        ]
    )
    unsupported = _unique(
        [
            *profile.unsupported_claims_flagged,
            *(opportunity_record.unsupported_claims_flagged if opportunity_record else []),
        ]
    )
    return _build_result(
        contract_name="business_research_analyst_to_outreach_composer",
        from_agent="business_research_analyst",
        to_agent="outreach_composer",
        required_fields=required_fields,
        allowed_optional_fields=_optional_field_values_present(profile, optional_fields),
        source_ids_required=source_ids,
        source_ids_present=[
            *profile_source_ids,
            *_source_ids_from_claims(profile.claims),
            *_source_ids_from_research_data_points(profile.research_data_points),
            *opportunity_source_ids,
            *(_source_ids_from_claims(opportunity_record.claims) if opportunity_record else []),
        ],
        unsupported_claims_required=unsupported,
        unsupported_claims_present=unsupported,
        missing_evidence_required=missing,
        missing_evidence_present=missing,
        issues=issues,
        audit_notes=[
            "Company profile fields needed for outreach are source-backed and separated "
            "from missing evidence and risk flags."
        ],
    )


def validate_outreach_composer_to_orchestrator(
    draft: OutreachDraft,
) -> HandoffContractResult:
    """Validate Outreach Composer output before orchestration and approval review."""

    required_fields = [
        "company_name",
        "email_subject",
        "email_body",
        "personalization_rationale",
        "facts_used",
        "source_ids_used",
        "unsupported_claims_flagged",
        "unsupported_claim_explanations",
        "approval_required",
        "approval_state",
        "approval_scope",
        "send_enabled",
        "sent",
        "can_send_email",
    ]
    optional_fields = [
        "recipient",
        "contact_name",
        "contact_title",
        "linkedin_note",
        "blocked_facts",
        "outreach_context",
        "style_profile_used",
        "style_profile_id",
        "template_id",
        "template_version",
        "template_fit_reason",
        "example_ids_used",
        "example_guidance_used",
        "call_prep",
        "follow_up_schedules",
        "revision_request",
        "draft_policy",
    ]
    fact_source_ids = _source_ids_from_claims(draft.facts_used)
    present_source_ids = _unique(draft.source_ids_used)
    style_source_ids = _unique(
        [
            draft.template_id,
            draft.template_version,
            draft.style_profile_id,
            *draft.example_ids_used,
        ]
    )
    issues = [
        *_required_field_issues(
            draft,
            fields=required_fields,
            non_empty_fields=[
                "company_name",
                "email_subject",
                "email_body",
                "personalization_rationale",
                "facts_used",
                "source_ids_used",
            ],
        )
    ]
    style_ids_in_sources = sorted(set(style_source_ids).intersection(present_source_ids))
    if style_ids_in_sources:
        issues.append(
            HandoffIssue(
                field="source_ids_used",
                message=(
                    "Tone, template, or example IDs were mixed into factual source IDs: "
                    f"{', '.join(style_ids_in_sources)}."
                ),
            )
        )
    if draft.unsupported_claims_flagged and not draft.unsupported_claim_explanations:
        issues.append(
            HandoffIssue(
                field="unsupported_claim_explanations",
                message="Unsupported outreach claim flags must include explanations.",
            )
        )
    if (
        not draft.approval_required
        or draft.approval_state != ApprovalState.PENDING
        or draft.approval_scope != ApprovalScope.EXTERNAL_USE
    ):
        issues.append(
            HandoffIssue(
                field="approval_state",
                message="Outreach handoff must remain pending external-use approval.",
            )
        )
    if draft.send_enabled or draft.sent or draft.can_send_email:
        issues.append(
            HandoffIssue(
                field="send_enabled",
                message="Outreach handoff must not enable sending or mark copy as sent.",
            )
        )
    return _build_result(
        contract_name="outreach_composer_to_orchestrator",
        from_agent="outreach_composer",
        to_agent="orchestrator",
        required_fields=required_fields,
        allowed_optional_fields=_optional_field_values_present(draft, optional_fields),
        source_ids_required=fact_source_ids,
        source_ids_present=present_source_ids,
        unsupported_claims_required=draft.unsupported_claims_flagged,
        unsupported_claims_present=draft.unsupported_claims_flagged,
        missing_evidence_required=draft.blocked_facts,
        missing_evidence_present=draft.blocked_facts,
        issues=issues,
        audit_notes=[
            "Supported factual claims remain separate from tone, style, template, and "
            "example guidance metadata."
        ],
    )


def validate_orchestrator_to_approval_review(
    result: OrchestratorResult,
) -> HandoffContractResult:
    """Validate Orchestrator metadata before Slack or approval review."""

    required_fields = [
        "route",
        "target_agent",
        "rationale",
        "approval_required",
        "approval_state",
        "approval_scope",
        "external_use_approval_required",
        "send_enabled",
        "can_send_email",
        "forbidden_actions",
        "intended_handoffs",
        "workflow_state_summary",
        "audit_notes",
    ]
    optional_fields = [
        "workflow",
        "routing_mode",
        "clarification_request",
        "stop_reason",
        "requires_human_review",
        "approved_context_present",
        "refused",
        "artifacts",
        "state_context_used",
        "operator_feedback_request",
    ]
    issues = [
        *_required_field_issues(
            result,
            fields=required_fields,
            non_empty_fields=["route", "rationale", "forbidden_actions", "audit_notes"],
        )
    ]
    if result.route != "clarification" and not result.target_agent:
        issues.append(
            HandoffIssue(
                field="target_agent",
                message="Non-clarification orchestrator routes must name the target SDK agent.",
            )
        )
    if result.send_enabled or result.can_send_email:
        issues.append(
            HandoffIssue(
                field="send_enabled",
                message="Orchestrator approval review handoff must not enable sending.",
            )
        )
    if "send_email" not in result.forbidden_actions:
        issues.append(
            HandoffIssue(
                field="forbidden_actions",
                message="Orchestrator handoff must preserve send_email as a forbidden action.",
            )
        )
    if result.route != "clarification" and not result.intended_handoffs:
        issues.append(
            HandoffIssue(
                field="intended_handoffs",
                message="Orchestrator must preserve explicit SDK handoff metadata.",
            )
        )
    artifact_source = (
        result.artifacts.model_dump(mode="json")
        if isinstance(result.artifacts, BaseModel)
        else result.artifacts
    )
    artifact_ids = _unique(artifact_source.keys() if isinstance(artifact_source, dict) else [])
    return _build_result(
        contract_name="orchestrator_to_approval_review",
        from_agent="orchestrator",
        to_agent="approval_review",
        required_fields=required_fields,
        allowed_optional_fields=_optional_field_values_present(result, optional_fields),
        source_ids_required=artifact_ids,
        source_ids_present=artifact_ids,
        unsupported_claims_required=[],
        unsupported_claims_present=[],
        missing_evidence_required=[],
        missing_evidence_present=[],
        issues=issues,
        audit_notes=[
            "Orchestrator review metadata is preserved for human approval without enabling "
            "external side effects."
        ],
    )


def validate_pipeline_handoff_contracts(
    *,
    company_profile: CompanyProfile | None = None,
    opportunity_record: ScoutOpportunityRecord | None = None,
    outreach_draft: OutreachDraft | None = None,
) -> list[HandoffContractResult]:
    """Validate every available handoff in a dry-run pipeline result."""

    results: list[HandoffContractResult] = []
    if opportunity_record is not None:
        results.append(validate_opportunity_to_business_research_analyst(opportunity_record))
    if company_profile is not None:
        results.append(
            validate_business_research_analyst_to_outreach_composer(
                company_profile,
                opportunity_record=opportunity_record,
            )
        )
    if outreach_draft is not None:
        results.append(validate_outreach_composer_to_orchestrator(outreach_draft))
    return results


def raise_for_invalid_handoffs(results: Sequence[HandoffContractResult]) -> None:
    """Raise a compact error if any required handoff contract is invalid."""

    invalid = [result for result in results if not result.valid]
    if not invalid:
        return
    messages = []
    for result in invalid:
        issues = "; ".join(issue.message for issue in result.issues if issue.severity == "error")
        messages.append(f"{result.contract_name}: {issues}")
    raise HandoffContractError("Invalid Keystone handoff contract: " + " | ".join(messages))


def handoff_contracts_metadata(
    results: Sequence[HandoffContractResult],
    *,
    contract_name: HandoffContractName | None = None,
) -> list[dict[str, Any]]:
    """Return compact, safe handoff metadata for logs and approval records."""

    return [
        result.to_approval_metadata()
        for result in results
        if contract_name is None or result.contract_name == contract_name
    ]
