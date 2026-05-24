from __future__ import annotations

from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
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


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'context-packs.db'}"


def _save_memory(store: SQLiteStore, **overrides) -> int:
    item = MemoryItem(
        memory_type=overrides.pop("memory_type", "company_fact"),
        object_type=overrides.pop("object_type", "company"),
        object_id=overrides.pop("object_id", "NeuroFlow"),
        object_key=overrides.pop("object_key", "NeuroFlow"),
        title=overrides.pop("title", "NeuroFlow fact"),
        summary=overrides.pop("summary", "NeuroFlow has a prior source-backed fact."),
        source_ids=overrides.pop("source_ids", ["fixture:memory"]),
        approval_state=overrides.pop("approval_state", ApprovalState.APPROVED_FOR_RESEARCH),
        confidence=overrides.pop("confidence", 0.8),
        safe_for_prompt=overrides.pop("safe_for_prompt", True),
        content=overrides.pop("content", {"claim_text": "Prior source-backed fact."}),
        **overrides,
    )
    return store.save_memory_item(item)


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


def test_research_context_pack_hydrates_approved_prompt_safe_company_memory(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    _save_memory(
        store,
        title="NeuroFlow source-backed positioning",
        summary="Previously reviewed source-backed NeuroFlow positioning.",
        content={"claim_text": "NeuroFlow supports behavioral health care navigation."},
    )
    _save_memory(
        store,
        title="Pending NeuroFlow note",
        summary="This pending memory should not hydrate.",
        approval_state=ApprovalState.PENDING,
    )
    _save_memory(
        store,
        title="Unsafe NeuroFlow note",
        summary="This unsafe memory should not hydrate.",
        safe_for_prompt=False,
    )
    _save_memory(
        store,
        memory_type="retrieval_tool_performance",
        object_type="workflow",
        object_id="work_item_company_research:NeuroFlow",
        object_key="NeuroFlow",
        title="NeuroFlow retrieval provider note",
        summary="SearXNG returned useful NeuroFlow sources.",
        content={"provider": "searxng", "success_rate": 1.0},
    )
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research NeuroFlow",
        request_text="research NeuroFlow",
        target=WorkItemTarget(name="NeuroFlow"),
    )

    pack = build_context_pack_for_route(
        item,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        store=store,
    )

    assert pack.ready is True
    assert len(pack.approved_company_facts) == 1
    assert pack.approved_company_facts[0].title == "NeuroFlow source-backed positioning"
    assert pack.approved_company_facts[0].source_ids == ["fixture:memory"]
    assert pack.approved_company_facts[0].content_summary["claim_text"].startswith("NeuroFlow")
    assert len(pack.retrieval_performance_notes) == 1
    hydrated_titles = {memory.title for memory in pack.relevant_memory_refs}
    assert "Pending NeuroFlow note" not in hydrated_titles
    assert "Unsafe NeuroFlow note" not in hydrated_titles


def test_opportunity_context_pack_hydrates_prior_opportunities_without_satisfying_sources(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    _save_memory(
        store,
        memory_type="opportunity_signal",
        object_type="opportunity",
        object_id="NeuroFlow",
        object_key="behavioral health AI companies",
        title="Prior NeuroFlow opportunity signal",
        summary="NeuroFlow was already reviewed as a behavioral health AI opportunity.",
        content={"opportunity_type": "partnership", "priority_score": 82},
    )
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Find partners",
        request_text="find behavioral health AI companies",
        target=WorkItemTarget(name="behavioral health AI companies", object_type="topic"),
    )

    pack = build_opportunity_context_pack(item)
    hydrated = build_context_pack_for_route(item, WorkItemRoute.OPPORTUNITY_SCOUT, store=store)

    assert pack.prior_opportunity_refs == []
    assert hydrated.ready is True
    assert hydrated.source_sufficiency == "missing"
    assert hydrated.prior_opportunity_refs[0].title == "Prior NeuroFlow opportunity signal"
    assert "A source ref, retrieval marker, or source-backed artifact is required." in (
        hydrated.limitation_notes
    )


def test_outreach_context_pack_hydrates_style_and_approval_history_without_gate_bypass(
    tmp_path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    _save_memory(
        store,
        memory_type="email_style_preference",
        object_type="email_style_profile",
        object_id="NeuroFlow",
        object_key="NeuroFlow",
        title="Approved concise outreach style",
        summary="Use concise source-backed framing.",
        content={"style_summary": "Concise source-backed outreach."},
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    _save_memory(
        store,
        memory_type="approval_decision",
        object_type="approval",
        object_id="NeuroFlow",
        object_key="NeuroFlow",
        title="Prior external-use approval lesson",
        summary="Manual approval required before external use.",
        content={"decision": "approved_for_external_use", "scope": "external_use"},
        approval_state=ApprovalState.APPROVED_FOR_EXTERNAL_USE,
    )
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

    pack = build_context_pack_for_route(item, WorkItemRoute.OUTREACH_COMPOSER, store=store)
    gate_by_name = {gate.name: gate for gate in pack.readiness_gates}

    assert pack.ready is True
    assert pack.outreach_style_examples[0].title == "Approved concise outreach style"
    assert pack.approval_history_refs[0].title == "Prior external-use approval lesson"
    assert gate_by_name["external_use_readiness"].ready is False
    assert "External use requires explicit human approval." in pack.limitation_notes


def test_gmail_context_pack_hydrates_reply_memory_from_thread_metadata(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    _save_memory(
        store,
        memory_type="human_feedback",
        object_type="feedback",
        object_id="thread-1",
        object_key="thread-1",
        title="Reply style feedback",
        summary="Keep replies short and source-backed.",
        content={"lessons": ["Keep replies short."]},
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    _save_memory(
        store,
        memory_type="approval_decision",
        object_type="approval",
        object_id="thread-1",
        object_key="thread-1",
        title="Prior Gmail approval",
        summary="Approved manual reply framing.",
        content={"decision": "approved_for_drafting", "scope": "drafting"},
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Reply to thread",
        target=WorkItemTarget(
            email="sender@example.com",
            metadata={
                "thread_id": "thread-1",
                "message_id": "msg-1",
                "reply_objective": "Send a concise follow-up.",
            },
        ),
    )

    pack = build_context_pack_for_route(item, WorkItemRoute.GMAIL_TRIAGE, store=store)

    assert pack.ready is True
    assert pack.outreach_style_examples[0].title == "Reply style feedback"
    assert pack.approval_history_refs[0].title == "Prior Gmail approval"
