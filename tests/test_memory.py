from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from keystone_agents.memory import (
    approval_decision_memory_item,
    build_chief_of_staff_memory_context,
    build_email_style_profile_from_feedback,
    check_workflow_duplicate,
    chief_of_staff_memory_item,
    company_profile_memory_items,
    email_style_memory_item,
    feedback_memory_item,
    manager_loop_efficiency_memory_item,
    opportunity_entity_memory_items,
    opportunity_memory_items,
    outreach_dedup_memory_items,
    outreach_example_memory_item,
    retrieval_tool_performance_memory_item,
)
from keystone_agents.outreach_examples import retrieve_outreach_examples_local
from keystone_agents.schemas.approval import (
    ApprovalDecisionRecord,
    ApprovalQueueItem,
    ApprovalState,
)
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.feedback import FeedbackRecord
from keystone_agents.schemas.opportunity import OpportunityRecord, OpportunitySource
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.schemas.outreach_examples import OutreachExampleRecord
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.memory_tool import (
    check_workflow_duplicate as check_workflow_duplicate_tool,
)
from keystone_agents.tools.memory_tool import (
    learn_email_style_profile,
    retrieve_memory,
    retrieve_outreach_examples,
    save_approval_decision_memory,
    save_entity_memory,
)


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'memory.db'}"


def _company_profile() -> CompanyProfile:
    return CompanyProfile(
        name="NeuroFlow",
        website="https://www.neuroflow.com",
        description="Behavioral health technology company.",
        fit_summary="Strong fit for behavioral health and evidence generation.",
        behavioral_health_relevance=92,
        evidence_generation_need=85,
        consulting_fit_score=88,
        confidence_score=0.86,
        sources=[
            SourceRecord(
                source_id="fixture:neuroflow",
                title="NeuroFlow fixture",
                url="fixture://neuroflow",
                source_type="fixture",
                supported_claims=[
                    "Behavioral health technology company.",
                    "Strong fit for behavioral health and evidence generation.",
                ],
                confidence=0.86,
            )
        ],
    )


def _opportunity() -> OpportunityRecord:
    return OpportunityRecord(
        company_name="NeuroFlow",
        opportunity_type="behavioral health AI",
        priority_score=90,
        why_now_signal="Recent outcomes evidence signal.",
        recommended_next_step="Run business research before drafting.",
        sources=[
            OpportunitySource(
                source_id="fixture:neuroflow-signal",
                title="NeuroFlow signal",
                url="fixture://neuroflow-signal",
                source_type="fixture",
                supported_signal="Recent outcomes evidence signal.",
            )
        ],
        keystone_fit_reason="Behavioral health outcomes work matches Keystone evidence support.",
        outside_consulting_likelihood=82,
        handoff_to_business_research_analyst=True,
    )


def _approved_draft() -> OutreachDraft:
    return OutreachDraft(
        company_name="NeuroFlow",
        contact_name="Dr. Example",
        email_subject="NeuroFlow evidence workflow discussion",
        email_body=(
            "Hi Dr. Example,\n\n"
            "I noticed NeuroFlow's behavioral health outcomes work. "
            "Happy to compare notes if useful.\n\n"
            "Best,\nKeystone"
        ),
        linkedin_note="Hi Dr. Example, open to compare notes on evidence workflows?",
        personalization_rationale="Uses approved source-backed NeuroFlow context.",
        source_ids_used=["fixture:neuroflow"],
        approval_required=True,
        approval_state="approved_for_external_use",
        approval_scope="external_use",
    )


def _outreach_example(
    example_id: str,
    *,
    approved: bool = True,
    company_types: list[str] | None = None,
    opportunity_types: list[str] | None = None,
    outreach_goals: list[str] | None = None,
    outreach_stages: list[str] | None = None,
    template_id: str = "clinical-ai-intro",
) -> OutreachExampleRecord:
    return OutreachExampleRecord(
        example_id=example_id,
        approved_for_drafting=approved,
        company_types=company_types or ["trial technology company"],
        opportunity_types=opportunity_types or ["clinical AI validation"],
        outreach_goals=outreach_goals or ["compare notes on clinical AI evaluation support"],
        outreach_stages=outreach_stages or ["cold_intro"],
        conversation_pattern=(
            "Open with one source-backed signal, connect it to evaluation work, "
            "then ask for a low-pressure exchange."
        ),
        effective_phrases=["compare notes", "practical fit", "source-backed evaluation"],
        cta_pattern="Ask whether a brief exchange would be useful.",
        follow_up_pattern="Follow up once with a concise reminder and a clear opt-out.",
        reply_pattern="If interested, ask for non-sensitive context and timing.",
        lessons_learned=["Specific evidence language beats generic healthcare AI positioning."],
        template_id=template_id,
        style_profile_id="default",
        source_ids=[f"fixture:{example_id}"],
    )


def test_company_and_opportunity_memory_are_source_backed_and_retrievable(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    for item in company_profile_memory_items(_company_profile()):
        store.save_memory_item(item)
    for item in opportunity_memory_items(_opportunity()):
        store.save_memory_item(item)

    records = store.retrieve_memory(
        "behavioral health evidence",
        object_key="NeuroFlow",
        approved_only=True,
    )

    assert records
    assert all(record.source_ids for record in records)
    assert any(record.memory_type == "company_profile_snapshot" for record in records)
    assert any(record.memory_type == "opportunity_signal" for record in records)
    assert all(record.send_enabled is False for record in records)


def test_email_style_learning_uses_approved_drafts_without_storing_raw_bodies(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    feedback = FeedbackRecord(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="good",
        tags=["excellent_personalization", "too_verbose"],
        notes="Keep the compare notes phrasing and stay concise.",
    )

    profile = build_email_style_profile_from_feedback(
        profile_id="anup-learned",
        drafts=[_approved_draft()],
        feedback_records=[feedback],
    )
    style_id = store.save_email_style_profile(profile)
    memory_id = store.save_memory_item(email_style_memory_item(profile))
    encoded = json.dumps(store.fetch_all("email_style_profiles"))

    assert style_id == 1
    assert memory_id == 1
    assert profile.approved_for_drafting is True
    assert "compare notes" in profile.preferred_phrases
    assert "No em dash" in profile.formatting_preferences
    assert _approved_draft().email_body not in encoded


def test_memory_tools_learn_and_retrieve_style_profile(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    feedback = FeedbackRecord(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="good",
        tags=["excellent_personalization"],
    )

    learned = json.loads(
        learn_email_style_profile(
            "tool-learned",
            json.dumps([_approved_draft().model_dump(mode="json")]),
            json.dumps([feedback.model_dump(mode="json")]),
            database_url=database_url,
        )
    )
    retrieved = json.loads(
        retrieve_memory(
            "compare notes",
            object_key="tool-learned",
            memory_types=["email_style_preference"],
            database_url=database_url,
        )
    )

    assert learned["send_enabled"] is False
    assert learned["raw_sent_email_bodies_included"] is False
    assert retrieved["records"][0]["memory_type"] == "email_style_preference"


def test_feedback_memory_captures_false_positive_search_learning(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    feedback = FeedbackRecord(
        object_type="opportunity",
        object_id="os-3",
        rating="okay",
        tags=["false positive search pattern", "weak sourcing"],
        notes=(
            "False positive: broad digital health query matched payer news with no "
            "behavioral health AI, advisory, or clinical research signal. Narrow future "
            "searches before ranking."
        ),
    )

    memory_id = store.save_memory_item(feedback_memory_item(feedback))
    records = store.retrieve_memory(
        "false positive behavioral health AI",
        object_key="os-3",
        memory_types=["human_feedback"],
        approved_only=True,
    )

    assert memory_id == 1
    assert records
    record = records[0]
    assert record.memory_type == "human_feedback"
    assert record.safe_for_prompt is True
    assert record.send_enabled is False
    assert record.raw_email_body_included is False
    assert "false_positive_search_pattern" in record.content["tags"]
    assert "False positive" in record.content["notes_summary"]
    assert "raw Gmail" not in json.dumps(record.prompt_context())


def test_opportunity_entity_memory_is_bounded_and_generalized(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    entity = {
        "entity_name": "NCT Example Trial",
        "entity_kind": "trial",
        "canonical_entity_key": "trial:nct-example",
        "opportunity_type": "grant or collaboration opportunity",
        "priority_score": 84,
        "why_now_signal": "Trial completion creates an evidence synthesis opening.",
        "recommended_next_step": "Review source-backed trial status before outreach.",
        "research_needed": ["Confirm sponsor and published results."],
        "sources": [{"source_id": "fixture:nct-example"}],
        "raw_email_body": "Private raw email body must not be stored.",
        "draft_text": "Raw draft text must not be stored.",
    }

    ids = [
        SQLiteStore(database_url).save_memory_item(item)
        for item in opportunity_entity_memory_items(entity)
    ]
    saved = json.loads(
        save_entity_memory(
            json.dumps({**entity, "canonical_entity_key": "trial:nct-tool"}),
            database_url=database_url,
        )
    )
    records = SQLiteStore(database_url).retrieve_memory(
        "evidence synthesis",
        object_key="trial:nct-example",
        memory_types=["opportunity_signal"],
        approved_only=True,
    )
    encoded = json.dumps([record.prompt_context() for record in records], sort_keys=True)

    assert ids == [1]
    assert saved["saved_ids"] == [2]
    assert saved["send_enabled"] is False
    assert records[0].object_key == "trial:nct example"
    assert records[0].content["entity_kind"] == "trial"
    assert records[0].source_ids == ["fixture:nct-example"]
    assert "Private raw email body" not in encoded
    assert "Raw draft text" not in encoded


def test_approval_decision_memory_stores_sanitized_outcome_only(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    decision = ApprovalDecisionRecord(
        object_type="outreach_draft",
        object_id="draft-123",
        decision="approved_for_external_use",
        scope="external_use",
        reviewer="Anup",
        notes="Approved the source-backed framing. Do not send automatically.",
        risk_flags=["manual send only"],
        source_agent="approval_review",
    )
    approval_item = ApprovalQueueItem(
        id="approval-draft-123",
        object_type="outreach_draft",
        object_id="draft-123",
        title="Draft for NCT Example Trial",
        summary="Review source-backed outreach draft.",
        draft_text="Private raw draft body must not be stored.",
        source_agent="outreach_composer",
    )
    outcome = {
        "status": "approved_for_manual_use",
        "summary": "Reviewer approved the framing for manual follow-up.",
        "next_step": "Prepare a manual follow-up after source review.",
        "lessons": ["Specific evidence language was useful."],
        "raw_email_body": "Private raw email body must not be stored.",
    }

    memory_id = SQLiteStore(database_url).save_memory_item(
        approval_decision_memory_item(
            decision,
            outcome=outcome,
            approval_item=approval_item,
        )
    )
    tool_result = json.loads(
        save_approval_decision_memory(
            json.dumps(decision.model_dump(mode="json")),
            outcome_json=json.dumps(outcome),
            approval_item_json=json.dumps(approval_item.model_dump(mode="json")),
            database_url=database_url,
        )
    )
    records = SQLiteStore(database_url).retrieve_memory(
        "manual follow-up",
        object_key="draft-123",
        memory_types=["approval_decision"],
        approved_only=True,
    )
    encoded = json.dumps([record.prompt_context() for record in records], sort_keys=True)

    assert memory_id == 1
    assert tool_result["saved_id"] == 2
    assert tool_result["send_enabled"] is False
    assert records[0].content["decision"] == "approved_for_external_use"
    assert records[0].content["outcome"]["outcome_status"] == "approved_for_manual_use"
    assert records[0].send_enabled is False
    assert records[0].raw_email_body_included is False
    assert "Private raw draft body" not in encoded
    assert "Private raw email body" not in encoded


def test_workflow_dedup_prevents_repeat_research_opportunity_and_outreach(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    for item in company_profile_memory_items(_company_profile()):
        store.save_memory_item(item)
    for item in opportunity_memory_items(_opportunity()):
        store.save_memory_item(item)
    for item in outreach_dedup_memory_items(_approved_draft()):
        store.save_memory_item(item)

    research = check_workflow_duplicate(
        action="company_research",
        company_name="NeuroFlow",
        database_url=database_url,
    )
    opportunity = check_workflow_duplicate(
        action="opportunity",
        company_name="NeuroFlow",
        database_url=database_url,
    )
    outreach = check_workflow_duplicate(
        action="outreach_company",
        company_name="NeuroFlow",
        contact="Dr. Example",
        database_url=database_url,
    )
    tool_result = json.loads(
        check_workflow_duplicate_tool(
            action="company_research",
            company_name="NeuroFlow",
            database_url=database_url,
        )
    )

    assert research["duplicate"] is True
    assert opportunity["duplicate"] is True
    assert outreach["duplicate"] is True
    assert tool_result["duplicate"] is True
    assert tool_result["send_enabled"] is False


def test_outreach_example_retrieval_is_approved_only(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_memory_item(outreach_example_memory_item(_outreach_example("approved")))
    store.save_memory_item(
        outreach_example_memory_item(_outreach_example("pending", approved=False))
    )

    result = retrieve_outreach_examples_local(
        outreach_goal="compare notes on clinical AI evaluation support",
        company_type="trial technology company",
        opportunity_type="clinical AI validation",
        selected_template="clinical-ai-intro",
        outreach_stage="cold_intro",
        database_url=database_url,
    )

    assert [record.example_id for record in result.records] == ["approved"]
    assert result.records[0].raw_body_included is False
    assert result.send_enabled is False


def test_outreach_example_retrieval_respects_top_k_limit(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    for index in range(4):
        store.save_memory_item(outreach_example_memory_item(_outreach_example(f"ex-{index}")))

    result = retrieve_outreach_examples_local(
        outreach_goal="clinical AI evaluation",
        company_type="trial technology company",
        opportunity_type="clinical AI validation",
        selected_template="clinical-ai-intro",
        outreach_stage="cold_intro",
        limit=2,
        database_url=database_url,
    )

    assert len(result.records) == 2


def test_outreach_example_retrieval_ranks_relevant_matches(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_memory_item(
        outreach_example_memory_item(
            _outreach_example(
                "generic",
                company_types=["general healthcare company"],
                opportunity_types=["clinical AI validation"],
                outreach_goals=["stay in touch"],
                outreach_stages=["follow_up"],
                template_id="generic-follow-up",
            )
        )
    )
    store.save_memory_item(
        outreach_example_memory_item(
            _outreach_example(
                "specific",
                company_types=["behavioral health AI startup"],
                opportunity_types=["clinical AI validation"],
                outreach_goals=["evidence generation and clinical AI evaluation support"],
                outreach_stages=["cold_intro"],
                template_id="clinical-ai-intro",
            )
        )
    )

    result = retrieve_outreach_examples_local(
        outreach_goal="clinical AI evaluation support",
        company_type="behavioral health AI startup",
        opportunity_type="clinical AI validation",
        selected_template="clinical-ai-intro",
        outreach_stage="cold_intro",
        database_url=database_url,
    )

    assert result.records[0].example_id == "specific"
    assert "template match" in result.records[0].match_reason
    assert result.records[0].match_score > result.records[-1].match_score


def test_outreach_example_retrieval_does_not_leak_raw_body_or_private_content(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_memory_item(outreach_example_memory_item(_outreach_example("safe")))
    store.save_memory_item(
        {
            "memory_type": "outreach_example",
            "object_type": "outreach_draft",
            "object_id": "unsafe",
            "object_key": "clinical-ai-intro",
            "title": "Unsafe private example",
            "summary": "Unsafe private example",
            "content": {
                "example_id": "unsafe",
                "approved_for_drafting": True,
                "conversation_pattern": "From: private@example.com",
                "effective_phrases": ["Private raw sentence should never appear"],
                "cta_pattern": "Call Alex at 555-123-4567",
                "follow_up_pattern": "Raw body private secret",
                "reply_pattern": "Patient Jane diagnosis details",
                "lessons_learned": ["unsafe"],
                "template_id": "clinical-ai-intro",
                "style_profile_id": "default",
                "raw_body": "Private raw body should never appear",
            },
            "approval_state": "approved_for_drafting",
            "safe_for_prompt": True,
            "confidence": 1.0,
        }
    )

    payload = retrieve_outreach_examples(
        outreach_goal="clinical AI evaluation support",
        company_type="trial technology company",
        opportunity_type="clinical AI validation",
        selected_template="clinical-ai-intro",
        outreach_stage="cold_intro",
        database_url=database_url,
    )
    data = json.loads(payload)
    encoded = json.dumps(data, sort_keys=True)

    assert [record["example_id"] for record in data["records"]] == ["safe"]
    assert "Private raw body should never appear" not in encoded
    assert "private@example.com" not in encoded
    assert "555-123-4567" not in encoded
    assert "Patient Jane" not in encoded
    assert data["records"][0]["raw_body_included"] is False


def test_outreach_example_retrieval_handles_no_matches(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url).save_memory_item(
        outreach_example_memory_item(_outreach_example("safe"))
    )

    result = retrieve_outreach_examples_local(
        outreach_goal="investor update",
        company_type="biotech tools company",
        opportunity_type="capital raise",
        selected_template="investor-update",
        outreach_stage="post_meeting",
        database_url=database_url,
    )

    assert result.records == []
    assert "No approved sanitized" in result.guidance
    assert result.raw_body_included is False


def test_retrieval_tool_performance_memory_item_is_prompt_safe() -> None:
    item = retrieval_tool_performance_memory_item(
        {
            "search_providers_used": ["searxng", "serper"],
            "website_extraction": {
                "providers_used": ["trafilatura", "firecrawl"],
                "page_count": 2,
                "claim_count": 6,
            },
            "retrieval_ladder": [
                {
                    "rung": "search_discovery",
                    "providers": ["searxng", "serper"],
                    "raw_result_count": 10,
                    "useful": True,
                },
                {
                    "rung": "website_extraction",
                    "providers": ["trafilatura"],
                    "claim_count": 6,
                    "useful": True,
                },
            ],
        },
        object_id="company_research:Mentavi",
    )

    assert item is not None
    assert item.memory_type == "retrieval_tool_performance"
    assert item.safe_for_prompt is True
    assert item.content["website_extraction"]["claim_count"] == 6
    assert "search discovery" in item.summary


def test_manager_loop_efficiency_memory_item_is_prompt_safe() -> None:
    item = manager_loop_efficiency_memory_item(
        {
            "schema": "keystone.manager_loop_efficiency.v1",
            "metric_name": "keystone.manager_loop.efficiency",
            "metric_version": "v1",
            "elapsed_seconds": 7.25,
            "latency_bucket": "5_to_15s",
            "step_count": 2,
            "specialist_step_count": 1,
            "repair_count": 1,
            "repair_rate": 0.5,
            "repair_attempts_by_route": {"business-research-analyst": 1},
            "route_sequence": ["business-research-analyst", "business-research-analyst"],
            "final_route": "business-research-analyst",
            "final_status": "active",
            "advanced": True,
            "artifact_count": 2,
            "blocker_count": 0,
            "live_search": True,
            "live_sdk": True,
            "final_synthesis_executed": True,
            "seconds_per_specialist_step": 7.25,
            "completion_without_blockers": True,
            "efficiency_signal": "completed_after_repair",
        },
        object_id="work_item:wi_example",
    )

    assert item is not None
    assert item.memory_type == "manager_loop_efficiency"
    assert item.safe_for_prompt is True
    assert item.content["metric_name"] == "keystone.manager_loop.efficiency"
    assert item.content["efficiency_signal"] == "completed_after_repair"
    assert "7.250s" in item.summary


def test_chief_of_staff_memory_context_excludes_expired_and_superseded(
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    old_id = store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_decision",
            title="Old Beacon decision",
            summary="Old decision should be superseded.",
            object_id="Beacon",
            object_key="Beacon",
            source_ids=["operator"],
        )
    )
    store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_decision",
            title="Current Beacon decision",
            summary="Current decision should remain visible.",
            object_id="Beacon",
            object_key="Beacon",
            source_ids=["operator"],
            supersedes_memory_id=old_id,
        )
    )
    store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_goal",
            title="Expired Beacon goal",
            summary="Expired goal should not be visible.",
            object_id="Beacon",
            object_key="Beacon",
            source_ids=["operator"],
            expires_at="2020-01-01T00:00:00Z",
        )
    )

    context = build_chief_of_staff_memory_context(
        query="Beacon decision goal",
        object_key="Beacon",
        route="project-context-review",
        database_url=database_url,
    )

    assert [item.title for item in context.records] == ["Current Beacon decision"]
    assert context.send_enabled is False


def test_memory_eligibility_precedes_ranking_and_limit(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    current_id = store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_goal", title="Current synthetic goal",
            summary="Current objective", object_key="synthetic", source_ids=["fixture:goal"],
        )
    )
    for expiry in ("2020-01-01T00:00:00Z", "not-a-timestamp"):
        store.save_memory_item(
            chief_of_staff_memory_item(
                memory_type="project_goal", title="Ineligible synthetic goal",
                summary="Current objective", object_key="synthetic",
                source_ids=["fixture:goal"], expires_at=expiry, confidence=1.0,
            )
        )

    direct = json.loads(retrieve_memory(
        query="objective", object_key="synthetic", limit=1, database_url=database_url,
    ))
    chief = build_chief_of_staff_memory_context(
        query="objective", object_key="synthetic", limit=1, database_url=database_url,
    )

    assert [record["id"] for record in direct["records"]] == [current_id]
    assert [record.id for record in chief.records] == [current_id]
    assert len(store.list_memory_items()) == 3


def test_memory_supersession_is_independent_of_search_terms(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    old_id = store.save_memory_item(chief_of_staff_memory_item(
        memory_type="project_goal", title="Prior synthetic goal", summary="Old vocabulary",
        object_key="synthetic", source_ids=["fixture:old"],
    ))
    store.save_memory_item(chief_of_staff_memory_item(
        memory_type="project_decision", title="Replacement decision", summary="New direction",
        object_key="synthetic", source_ids=["fixture:new"], supersedes_memory_id=old_id,
        expires_at="2020-01-01T00:00:00Z",
    ))

    assert store.retrieve_memory(query="vocabulary", memory_types=["project_goal"]) == []
    assert len(store.list_memory_items()) == 2


@pytest.mark.parametrize("replacement_state", [ApprovalState.PENDING, ApprovalState.REJECTED])
def test_unapproved_replacement_does_not_hide_current_memory(tmp_path, replacement_state) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    old_id = store.save_memory_item(chief_of_staff_memory_item(
        memory_type="project_goal", title="Current synthetic goal", summary="Current objective",
        object_key="synthetic", source_ids=["fixture:current"],
    ))
    store.save_memory_item(chief_of_staff_memory_item(
        memory_type="project_goal", title="Proposed replacement", summary="Other objective",
        object_key="synthetic", source_ids=["fixture:proposal"],
        supersedes_memory_id=old_id, approval_state=replacement_state,
    ))

    assert [item.id for item in store.retrieve_memory(object_key="synthetic")] == [old_id]


def test_memory_expiration_uses_one_utc_boundary(tmp_path, monkeypatch) -> None:
    from keystone_agents.storage import sqlite_store

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 10, 12, tzinfo=UTC)

    store = SQLiteStore(_database_url(tmp_path))
    for title, expiry in (
        ("expired exactly", "2026-09-10T12:00:00Z"),
        ("expired naive", "2026-09-10T12:00:00"),
        ("valid offset", "2026-09-10T08:00:01-04:00"),
    ):
        store.save_memory_item(chief_of_staff_memory_item(
            memory_type="project_goal", title=title, summary="Synthetic objective",
            source_ids=["fixture:clock"], expires_at=expiry,
        ))
    monkeypatch.setattr(sqlite_store, "datetime", FixedDatetime)

    assert [item.title for item in store.retrieve_memory()] == ["valid offset"]
