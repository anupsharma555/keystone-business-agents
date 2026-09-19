from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
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
    build_project_context_pack,
    build_research_context,
    build_research_context_pack,
    drafting_ready,
)


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'context-packs.db'}"


def test_context_packs_preserve_manual_plan_ask_shape() -> None:
    work_item = WorkItem(
        id="wi_ask_shape",
        title="Selected source review",
        request_text="Use only the selected source and return a table.",
        kind=WorkItemKind.RESEARCH_BRIEF,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(
            metadata={
                "manual_request_plan": {
                    "ask_shape": {
                        "ask_breadth": "narrow",
                        "source_type_preference": ["selected"],
                        "strict_filter_mode": "exact",
                        "output_form": "table",
                        "prior_context_dependency": "selected_context",
                        "permission_state": "read_only",
                        "stop_condition": "return_zero_without_broadening_if_no_exact_match",
                    }
                }
            }
        ),
    )

    context = build_context_pack_for_route(
        work_item, WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    )

    assert context.ask_shape.output_form == "table"
    assert context.ask_shape.source_type_preference == ["selected"]
    assert context.ask_shape.stop_condition == "return_zero_without_broadening_if_no_exact_match"
    assert context.adaptation_assessment is not None
    assert context.adaptation_assessment.status == "compatible"
    assert context.adaptation_assessment.missing_required_fields == []


def test_all_specialist_context_packs_preserve_manual_plan_constraints() -> None:
    constraints = [
        "current",
        "source backed",
        "do not draft outreach",
        "do not write externally",
    ]
    work_item = WorkItem(
        id="wi_manual_constraints",
        title="Preserve planner constraints",
        request_text="Use the interpreted request constraints.",
        kind=WorkItemKind.OPPORTUNITY,
        target=WorkItemTarget(
            name="KNI opportunities",
            metadata={"manual_constraints": constraints},
        ),
    )

    for route in (
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.OUTREACH_COMPOSER,
        WorkItemRoute.GMAIL_TRIAGE,
    ):
        pack = build_context_pack_for_route(work_item, route)
        assert pack.constraints == constraints


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
    assert pack.source_triage.has_evidence() is False
    assert pack.adaptation_assessment is None
    assert pack.model_dump(mode="json")["source_triage"] == {}
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
    assert pack.input_type == "keystone_agents.schemas.work_item.WorkItem"
    assert pack.satisfies_input_type == (
        "keystone_agents.schemas.context_pack.OpportunityContextPack"
    )
    assert pack.expected_output_type == (
        "keystone_agents.schemas.opportunity.OpportunityScoutResult"
    )
    assert pack.next_input_type == "keystone_agents.schemas.context_pack.ResearchContextPack"
    assert pack.type_compatibility_status == "compatible"
    assert pack.handoff_type_contract.target_input_type == pack.satisfies_input_type
    assert pack.handoff_type_contract.target_output_type == pack.expected_output_type


def test_project_context_fixture_is_typed_and_attached_without_loose_duplication() -> None:
    fixture_path = Path(__file__).parent / "fixtures/project_context_gmail_research_outreach.json"
    project_context = json.loads(fixture_path.read_text(encoding="utf-8"))
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Review synthetic partner inquiry",
        request_text="Review the selected thread and prepare the next safe action.",
        target=WorkItemTarget(
            name="Synthetic Partner",
            email="sender@example.com",
            metadata={
                "thread_id": "thread-synthetic",
                "project_context": project_context,
            },
        ),
    )

    project_pack = build_project_context_pack(item)
    assert project_pack is not None
    assert project_pack.ready is True
    assert project_pack.project_id == "project-kni-synthetic-001"
    assert project_pack.source_refs[0].source_id == (
        "fixture:gmail-thread:synthetic-partner"
    )
    assert project_pack.allowed_actions == project_context["allowed_actions"]
    assert project_pack.blocked_actions == project_context["blocked_actions"]

    pack = build_gmail_context_pack(item)
    assert pack.ready is True
    assert pack.project_context == project_pack
    assert "project_context" not in pack.summary["target"]["metadata"]
    assert pack.readiness_gates[0].name == "project_context_readiness"
    assert pack.readiness_gates[0].ready is True


def test_project_context_is_attached_beside_every_specialist_pack() -> None:
    project_context = {
        "project_id": "project-synthetic",
        "name": "Synthetic Project",
        "objective": "Evaluate one synthetic source-backed business opportunity.",
        "approved_for_agent_use": True,
        "contains_phi": False,
        "allowed_actions": ["research", "draft reply text"],
        "blocked_actions": ["send email"],
    }
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Synthetic Partner",
        request_text="Research Synthetic Partner",
        target=WorkItemTarget(
            name="Synthetic Partner",
            email="sender@example.com",
            metadata={
                "thread_id": "thread-synthetic",
                "project_context": project_context,
            },
        ),
    )

    packs = [
        build_research_context_pack(item),
        build_opportunity_context_pack(item),
        build_outreach_context_pack(item),
        build_gmail_context_pack(item),
    ]

    assert all(pack.project_context is not None for pack in packs)
    assert {pack.project_context.project_id for pack in packs} == {"project-synthetic"}
    assert all(pack.readiness_gates[0].name == "project_context_readiness" for pack in packs)


def test_project_context_blocked_actions_override_allowed_actions() -> None:
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Synthetic project",
        target=WorkItemTarget(
            name="Synthetic Partner",
            metadata={
                "project_context": {
                    "project_id": "project-synthetic",
                    "name": "Synthetic Project",
                    "objective": "Prepare an internal summary.",
                    "approved_for_agent_use": True,
                    "allowed_actions": ["prepare internal summary", "send email"],
                    "blocked_actions": ["send email"],
                }
            },
        ),
    )

    project_pack = build_project_context_pack(item)
    assert project_pack is not None
    assert project_pack.allowed_actions == ["prepare internal summary"]
    assert project_pack.blocked_actions == ["send email"]


def test_unapproved_or_phi_project_context_blocks_specialist_readiness() -> None:
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Synthetic project",
        request_text="Research Synthetic Partner",
        target=WorkItemTarget(
            name="Synthetic Partner",
            metadata={
                "project_context": {
                    "project_id": "project-synthetic",
                    "name": "Synthetic Project",
                    "objective": "Prepare an internal summary.",
                    "approved_for_agent_use": False,
                    "contains_phi": True,
                }
            },
        ),
    )

    pack = build_research_context_pack(item)

    assert pack.ready is False
    assert pack.project_context is not None
    assert pack.project_context.ready is False
    assert {blocker.code for blocker in pack.project_context.blockers} == {
        "project_context_not_approved",
        "project_context_contains_phi",
    }
    assert pack.missing_requirements[:2] == [
        "Approve this bounded project context before specialist use.",
        "Project context containing PHI or patient-specific data cannot be used.",
    ]


def test_absent_project_context_does_not_add_friction() -> None:
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Synthetic Partner",
        target=WorkItemTarget(name="Synthetic Partner"),
    )

    pack = build_research_context_pack(item)

    assert pack.project_context is None
    assert [gate.name for gate in pack.readiness_gates] == ["research_target_readiness"]
    assert pack.ready is True


def test_context_pack_payload_preserves_ordered_sources_for_followups() -> None:
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI mental health",
        request_text="summarize link 1 from the prior thread",
        target=WorkItemTarget(name="OpenAI"),
        sources=[
            WorkItemSourceRef(
                title="OpenAI mental health update",
                url="https://openai.com/index/update-on-mental-health-related-work/",
                source_type="company_site",
                supported_claim="OpenAI describes mental-health-related safety work.",
                evidence_excerpt="OpenAI is improving responses in sensitive conversations.",
                extraction_status="article_read",
            )
        ],
    )

    payload = build_research_context(item)

    assert payload["satisfies_input_type"] == (
        "keystone_agents.schemas.context_pack.ResearchContextPack"
    )
    assert payload["expected_output_type"] == "keystone_agents.schemas.research.ResearchBrief"
    assert payload["next_input_type"] == (
        "keystone_agents.schemas.context_pack.OutreachContextPack"
    )
    assert payload["handoff_type_contract"]["compatibility_status"] == "compatible"
    assert payload["ordered_sources"] == [
        {
            "index": 1,
            "reference": "source 1",
            "title": "OpenAI mental health update",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "company_site",
            "extraction_status": "article_read",
            "supported_claim": "OpenAI describes mental-health-related safety work.",
            "evidence_excerpt": "OpenAI is improving responses in sensitive conversations.",
        }
    ]


def test_context_pack_carries_source_triage_for_specialist_reasoning() -> None:
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI mental health",
        request_text="What is OpenAI doing about mental health?",
        target=WorkItemTarget(name="OpenAI"),
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="1",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="OpenAI",
                summary="Source-backed profile.",
                metadata={
                    "retrieval_diagnostics": {
                        "source_triage": {
                            "mode": "fixture_safe_source_triage",
                            "recommended_action": "broaden_or_deepen_before_final_synthesis",
                            "needs_broaden_or_deepen": True,
                            "retained_source_ids": [],
                            "review_source_ids": [],
                            "rejected_source_ids": ["source:2"],
                            "deepen_source_ids": ["source:1"],
                            "recall_gaps": ["missing expected source lane: press_news"],
                            "decisions": [
                                {
                                    "source_id": "source:1",
                                    "title": "OpenAI mental health update",
                                    "url": (
                                        "https://openai.com/index/"
                                        "update-on-mental-health-related-work/"
                                    ),
                                    "decision": "deepen",
                                    "relevance_score": 82,
                                    "directness_score": 60,
                                    "rationale": "deepen: promising match needs page extraction",
                                },
                                {
                                    "source_id": "source:2",
                                    "title": "OpenAI unrelated partnership",
                                    "url": "https://example.com/openai-partnership",
                                    "decision": "reject",
                                    "relevance_score": 8,
                                    "directness_score": 40,
                                    "rationale": "reject: low request-term overlap",
                                },
                            ],
                        }
                    }
                },
            )
        ],
    )

    pack = build_context_pack_for_route(item, WorkItemRoute.BUSINESS_RESEARCH_ANALYST)

    assert pack.source_triage.recommended_action == (
        "broaden_or_deepen_before_final_synthesis"
    )
    assert pack.source_triage.needs_broaden_or_deepen is True
    assert pack.source_triage.decision_counts == {"deepen": 1, "reject": 1}
    assert pack.source_triage.deepen_source_ids == ["source:1"]
    assert pack.source_triage.rejected_source_ids == ["source:2"]
    assert pack.source_triage.decisions[0].decision == "deepen"
    assert pack.source_triage.recall_gaps == ["missing expected source lane: press_news"]
    assert pack.model_dump(mode="json")["source_triage"]["decisions"][0]["decision"] == "deepen"


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


def test_opportunity_context_pack_hydrates_prior_opportunities_without_satisfying_sources(
    tmp_path,
) -> None:
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


def test_model_view_removes_exact_mirrors_but_preserves_authority_and_unique_context():
    item = WorkItem(
        title="Review a selected message", kind=WorkItemKind.GMAIL_THREAD,
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        sources=[WorkItemSourceRef(source_id="selected", url="https://example.test/source",
                                  evidence_excerpt="Selected evidence " * 100)],
    )
    pack = build_outreach_context_pack(item)
    before = pack.model_dump(mode="json")
    pack.summary["unique_note"] = "An unresolved contradiction must remain visible."
    pack.summary["facts"] = [{"claim": "Unapproved context must not silently disappear."}]
    view = pack.without_duplicate_context()
    assert type(view) is type(pack)
    assert view.source_refs == pack.source_refs and view.retrieved_sources == []
    assert "sources" not in view.summary and "target" not in view.summary
    assert view.summary["facts"] == pack.summary["facts"]
    assert view.summary["unique_note"] == pack.summary["unique_note"]
    assert view.approval_gates == pack.approval_gates
    assert view.readiness_gates == pack.readiness_gates
    assert view.allowed_claims == pack.allowed_claims
    assert view.missing_requirements == pack.missing_requirements
    assert pack.model_dump(mode="json")["source_refs"] == before["source_refs"]
    assert "sources" in pack.summary and pack.retrieved_sources == pack.source_refs
    # A different version of retrieved evidence is not an exact duplicate.
    pack.retrieved_sources = [
        pack.source_refs[0].model_copy(update={"evidence_excerpt": "Correction"})
    ]
    assert pack.without_duplicate_context().retrieved_sources == pack.retrieved_sources
