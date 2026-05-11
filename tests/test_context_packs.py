from __future__ import annotations

from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.work_items import (
    approve_artifact_context,
    build_context_pack_for_route,
    build_gmail_context_pack,
    build_opportunity_context_pack,
    build_outreach_context,
    build_outreach_context_pack,
    build_research_context_pack,
    drafting_ready,
)


def test_research_context_pack_requires_target() -> None:
    item = WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Research")

    pack = build_research_context_pack(item)

    assert pack.ready is False
    assert pack.can_synthesize is False
    assert pack.readiness_gates[0].name == "research_target_readiness"
    assert pack.readiness_gates[0].blockers[0].code == "missing_research_target"
    assert pack.missing_requirements == ["A company, domain, URL, or research target is required."]


def test_opportunity_context_pack_keeps_source_sufficiency_optional_for_initial_scan() -> None:
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Find partners",
        request_text="find behavioral health AI companies",
        target=WorkItemTarget(name="behavioral health AI companies", object_type="topic"),
    )

    pack = build_opportunity_context_pack(item)

    assert pack.ready is True
    assert pack.can_synthesize is True
    assert pack.objective == "find behavioral health AI companies"
    assert [gate.name for gate in pack.readiness_gates] == [
        "opportunity_objective_clarity",
        "source_sufficiency",
    ]
    assert pack.readiness_gates[1].required is False
    assert pack.missing_requirements == []
    assert pack.limitation_notes == [
        "A source ref, retrieval marker, or source-backed artifact is required."
    ]


def test_outreach_context_pack_applies_recipient_claim_source_and_research_gates() -> None:
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        title="Draft outreach",
        target=WorkItemTarget(name="NeuroFlow"),
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="1",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="NeuroFlow",
                summary="Digital behavioral health company.",
                selected=True,
            )
        ],
    )

    blocked_pack = build_outreach_context_pack(item)

    assert blocked_pack.ready is False
    assert blocked_pack.can_synthesize is False
    assert blocked_pack.contact.organization == "NeuroFlow"
    assert {gate.name: gate.ready for gate in blocked_pack.readiness_gates if gate.required} == {
        "outreach_recipient_readiness": True,
        "approved_claims_readiness": False,
        "source_sufficiency": True,
        "research_sufficiency": True,
    }
    assert drafting_ready(item).blockers[0].code == "outreach_requires_approved_context"
    assert blocked_pack.missing_requirements == [
        "Drafting requires approved facts or selected artifacts approved for drafting."
    ]

    approved = approve_artifact_context(
        item,
        "company_profile",
        "1",
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    ready_pack = build_outreach_context_pack(approved)

    assert ready_pack.ready is True
    assert ready_pack.can_synthesize is True
    assert drafting_ready(approved).ready is True
    assert ready_pack.approval_state == ApprovalState.APPROVED_FOR_DRAFTING.value


def test_outreach_context_pack_marks_missing_direct_contact_channel_as_optional_gap() -> None:
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        title="Draft outreach",
        target=WorkItemTarget(name="NeuroFlow"),
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="1",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
                title="NeuroFlow",
                summary="Digital behavioral health company.",
                selected=True,
                metadata={"source_count": 2},
            )
        ],
    )

    pack = build_outreach_context_pack(item)
    gate_by_name = {gate.name: gate for gate in pack.readiness_gates}

    assert pack.ready is True
    assert pack.can_synthesize is True
    assert pack.contact.readiness_level == "organization_only"
    assert "contact email" in pack.contact.missing
    assert "LinkedIn URL" in pack.contact.missing
    assert gate_by_name["outreach_contact_channel_readiness"].required is False
    assert gate_by_name["outreach_contact_channel_readiness"].ready is False
    assert "A source-backed email address or LinkedIn URL is not available yet." in (
        pack.limitation_notes
    )
    assert "External use requires explicit human approval." in pack.limitation_notes


def test_legacy_context_helper_returns_typed_pack_payload_with_agent_key() -> None:
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        title="Draft outreach",
        target=WorkItemTarget(name="NeuroFlow"),
    )

    payload = build_outreach_context(item)

    assert payload["pack_type"] == "outreach"
    assert payload["agent"] == WorkItemRoute.OUTREACH_COMPOSER.value
    assert "readiness_gates" in payload


def test_gmail_context_pack_uses_thread_or_message_metadata() -> None:
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Reply to thread",
        target=WorkItemTarget(
            email="sender@example.com",
            metadata={
                "thread_id": "thread-1",
                "message_id": "msg-1",
                "thread_summary": "Sender asked for a follow-up.",
                "reply_objective": "Send a concise follow-up.",
                "risk_flags": ["legal_review"],
            },
        ),
    )

    pack = build_gmail_context_pack(item)

    assert pack.ready is True
    assert pack.thread_id == "thread-1"
    assert pack.message_id == "msg-1"
    assert pack.sender == "sender@example.com"
    assert pack.risk_flags == ["legal_review"]


def test_build_context_pack_for_route_selects_specialist_pack() -> None:
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Find partners",
        request_text="find clinical AI pilots",
    )

    pack = build_context_pack_for_route(item, WorkItemRoute.OPPORTUNITY_SCOUT)

    assert pack.pack_type == "opportunity"
    assert pack.route == WorkItemRoute.OPPORTUNITY_SCOUT
