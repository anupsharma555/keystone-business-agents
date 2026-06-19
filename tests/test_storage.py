from __future__ import annotations

import json
import sqlite3

import pytest

from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    ApprovalTransitionError,
    state_allows_drafting,
    state_allows_external_use,
    state_allows_sending,
    validate_approval_transition,
)
from keystone_agents.schemas.company_profile import (
    CompanyFeatureRecord,
    CompanyProfile,
    SourceRecord,
)
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.opportunity import OpportunityRecord, OpportunitySource
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import (
    FollowUpScheduleRecord,
    OutreachDraft,
    OutreachTrackingRecord,
    build_initial_outreach_tracking_record,
)
from keystone_agents.schemas.outreach_examples import OutreachExampleDocument
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import (
    CURRENT_SCHEMA_VERSION,
    OUTREACH_EMAIL_BODY_STORAGE_NOTE,
    SCHEMA_MIGRATIONS,
    SQLiteStore,
    redact_secrets,
    sqlite_path_from_url,
)
from keystone_agents.tools.email_style_tool import load_email_style_profile
from keystone_agents.tools.storage_tool import (
    StorageTool,
    load_approved_contact_context,
    load_approved_crm_context,
    load_pending_approval_items,
)

EXPECTED_TABLES = {
    "agent_runs",
    "emails",
    "companies",
    "opportunities",
    "outreach_drafts",
    "approvals",
    "sources",
    "tool_events",
    "agent_run_logs",
    "contacts",
    "crm_contexts",
    "follow_up_schedules",
    "outreach_tracking",
    "email_style_profiles",
    "memory_items",
    "memory_index",
    "outreach_examples",
    "outreach_example_fts",
    "work_items",
    "work_item_events",
    "work_item_artifacts",
    "automation_specs",
    "automation_runs",
    "automation_channel_bindings",
    "automation_findings",
    "announcement_feed_items",
    "announcement_feed_evidence",
    "announcement_feed_fts",
    "schema_migrations",
}


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'keystone_test.db'}"


def _expected_migration_versions() -> list[int]:
    return [version for version, _, _ in SCHEMA_MIGRATIONS]


def _email_result() -> EmailTriageResult:
    return EmailTriageResult(
        message_id="msg-1",
        subject="Clinical AI discussion",
        sender_email="alex@example.com",
        category="consulting_opportunity",
        confidence=0.91,
        priority="high",
        summary="Sender asks about a clinical AI evaluation discussion.",
        reasoning="Business inquiry with no PHI.",
        needs_reply=True,
        recommended_labels=["Keystone/Action Required"],
        recommended_action="Create draft for human approval.",
        draft_reply="Thanks for reaching out. Please send non-sensitive context.",
        draft_created=True,
        approval_required=True,
    )


def _company_profile() -> CompanyProfile:
    return CompanyProfile(
        name="NeuroFlow",
        website="https://www.neuroflow.com",
        description="Behavioral health technology company.",
        fit_summary="Relevant to behavioral health analytics.",
        consulting_fit_score=75,
        confidence_score=0.82,
        sources=[
            SourceRecord(
                source_id="fixture:neuroflow",
                title="Fixture profile",
                url="fixture://neuroflow",
                source_type="fixture",
                supported_claims=["Behavioral health technology company."],
                confidence=0.8,
            )
        ],
        features=[
            CompanyFeatureRecord(
                feature_name="market_segment",
                value="behavioral health technology",
                source_id="fixture:neuroflow",
                evidence_text="Behavioral health technology company.",
                confidence=0.8,
            )
        ],
    )


def _opportunity_record() -> OpportunityRecord:
    return OpportunityRecord(
        company_name="NeuroFlow",
        opportunity_type="behavioral health AI",
        priority_score=88,
        why_now_signal="Payer partnership and outcomes evidence.",
        recommended_next_step="Hand off to business research before outreach.",
        sources=[
            OpportunitySource(
                title="Fixture signal",
                url="fixture://signal",
                source_type="fixture",
                supported_signal="Payer partnership.",
            )
        ],
        source_signals=["payer partnership"],
        keystone_fit_reason="Relevant to behavioral health and evidence generation.",
        outside_consulting_likelihood=80,
        handoff_to_business_research_analyst=True,
    )


def _outreach_draft() -> OutreachDraft:
    return OutreachDraft(
        company_name="NeuroFlow",
        contact_name="Dr. Example",
        email_subject="NeuroFlow clinical AI workflow discussion",
        email_body="Hello, I noticed NeuroFlow's public behavioral health analytics work.",
        linkedin_note="Hello, open to a brief exchange on clinical AI workflows?",
        personalization_rationale="Uses approved fixture company context.",
        approval_required=True,
        approval_state="pending",
        approval_scope="send",
    )


def _follow_up_schedule() -> FollowUpScheduleRecord:
    return FollowUpScheduleRecord(
        company_name="NeuroFlow",
        contact_name="Dr. Example",
        related_draft_id="1",
        proposed_date="2026-05-01",
        sequence_number=1,
        status="recommended",
        rationale="Data-only follow-up recommendation after human draft review.",
        approval_required=True,
    )


def _outreach_tracking() -> OutreachTrackingRecord:
    return OutreachTrackingRecord(
        draft_id="1",
        company_name="NeuroFlow",
        contact_name="Dr. Example",
        lifecycle_status="sent_manually",
        outreach_sent=True,
        sent_at="2026-05-01T14:00:00Z",
        sent_by="Anup",
        sent_via="manual_gmail",
        reply_received=False,
        outcome="pending_reply",
        next_step="Wait for reply before follow-up.",
    )


def _email_style_profile() -> EmailStyleProfile:
    return EmailStyleProfile(
        profile_id="neuroflow-style",
        source="fixture",
        source_id="fixture:neuroflow_style",
        source_url="fixture://neuroflow_style",
        confidence=0.8,
        approval_state="approved_for_drafting",
        approval_scope="drafting",
        greeting_patterns=["Hello {name},"],
        signoffs=["Warmly,"],
        sentence_length="medium",
        average_sentence_words=14,
        directness="high",
        cta_style="calendar_offer",
        formality="professional",
        formatting_preferences=["Short paragraphs"],
        preferred_phrases=["compare notes"],
        avoided_phrases=["circle back"],
        approved_sample_snippets=["Happy to compare notes if useful."],
        raw_sent_email_bodies_included=False,
    )


def _outreach_example_document(
    example_id: str,
    *,
    approved_for_drafting: bool = True,
) -> OutreachExampleDocument:
    return OutreachExampleDocument(
        example_id=example_id,
        source_thread_id_hash=f"{example_id}-hash-1234567890",
        source_label="sanitized private example fixture",
        company_type="trial technology company",
        opportunity_type="clinical AI validation",
        template_id="clinical-ai-intro",
        style_profile_id="default",
        sanitized_summary="Approved clinical AI validation outreach pattern.",
        conversation_pattern=(
            "Open with one source-backed clinical AI signal, then ask to compare notes."
        ),
        effective_phrases=["compare notes", "clinical AI evaluation"],
        cta_pattern="Ask whether a brief exchange would be useful.",
        follow_up_pattern="One concise manual follow-up after review.",
        reply_pattern="Ask for non-sensitive context if the recipient replies.",
        lessons_learned=["Use reusable structure without importing facts from the example."],
        approved_for_drafting=approved_for_drafting,
        raw_body_included=False,
    )


def _contact_record() -> ContactRecord:
    return ContactRecord(
        company_name="NeuroFlow",
        contact_name="Dr. Local Contact",
        role_title="Clinical Partnerships Lead",
        source="fixture",
        source_id="fixture:neuroflow_contact",
        source_url="fixture://neuroflow_contact",
        confidence=0.8,
        approval_state="approved_for_drafting",
        approval_scope="drafting",
        notes="Approved local contact context.",
    )


def _crm_account_context() -> CRMAccountContext:
    return CRMAccountContext(
        company_name="NeuroFlow",
        account_stage="researching",
        account_owner="Keystone fixture owner",
        last_touchpoint="No outbound contact made.",
        next_step="Draft only after approval.",
        source="fixture",
        source_id="fixture:neuroflow_crm_context",
        source_url="fixture://neuroflow_crm_context",
        confidence=0.75,
        approval_state="approved_for_drafting",
        approval_scope="drafting",
        notes="Approved local CRM account context.",
    )


def test_contact_record_accepts_local_contacts_table_aliases() -> None:
    contact = ContactRecord.model_validate(
        {
            "company": "NeuroFlow",
            "name": "Dr. Alias Contact",
            "title": "Clinical Partnerships Lead",
            "email": "",
            "profile_url": "fixture://profile/alias-contact",
            "source": "fixture",
            "confidence": 0.72,
            "approval_status": "approved_for_drafting",
            "notes": "Approved local contact context.",
        }
    )

    assert contact.company_name == "NeuroFlow"
    assert contact.name == "Dr. Alias Contact"
    assert contact.title == "Clinical Partnerships Lead"
    assert contact.email is None
    assert contact.profile_url == "fixture://profile/alias-contact"
    assert contact.approval_status == "approved_for_drafting"


def test_database_initializes_expected_tables(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    assert EXPECTED_TABLES <= store.table_names()


def test_announcement_feed_items_dedupe_by_publication_id_and_query(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = AnnouncementFeedItem(
        title="AI psychiatry preprint",
        url="https://doi.org/10.1101/2026.01.01.123456?utm_source=newsletter",
        source="bioRxiv",
        feed="preprints",
        doi="10.1101/2026.01.01.123456",
        tags=["clinical_ai"],
        selected=True,
        selection_reason="Relevant to clinical AI evaluation.",
        summary="Public preprint lead for clinical AI review.",
        evidence=[
            AnnouncementFeedEvidence(
                kind="article",
                title="AI psychiatry preprint",
                url="https://example.org/article",
                snippet="The article describes clinical AI review methods.",
                source="trafilatura",
                status="success",
                char_count=1200,
            )
        ],
    )

    first_key = store.save_announcement_feed_item(item)
    second_key = store.save_announcement_feed_item(item.model_copy(update={"summary": "Updated"}))
    results = store.list_announcement_feed_items(query="psychiatry")
    retrieved = store.retrieve_announcement_feed_items("clinical AI review")

    assert first_key == second_key == "doi:10.1101/2026.01.01.123456"
    assert len(results) == 1
    assert [item.canonical_key for item in retrieved] == [first_key]
    assert results[0].seen_count == 2
    assert results[0].canonical_url == "https://doi.org/10.1101/2026.01.01.123456"
    assert results[0].summary == "Updated"
    assert results[0].evidence[0].kind == "article"


def test_initialize_is_repeatable_and_records_schema_version(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.initialize()
    reopened = SQLiteStore(_database_url(tmp_path))
    migrations = reopened.fetch_all("schema_migrations")
    migration_names = {version: name for version, name, _ in SCHEMA_MIGRATIONS}

    assert reopened.current_schema_version() == CURRENT_SCHEMA_VERSION
    assert reopened.migration_versions() == _expected_migration_versions()
    assert len(migrations) == len(SCHEMA_MIGRATIONS)
    assert migrations[0]["version"] == 1
    assert migrations[0]["name"] == "initial_storage_schema"
    assert migrations[-1]["version"] == CURRENT_SCHEMA_VERSION
    assert all(row["name"] == migration_names[row["version"]] for row in migrations)


def test_tmp_path_database_url_works(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)

    assert sqlite_path_from_url(database_url).endswith("keystone_test.db")
    assert store.count("agent_runs") == 0


def test_outreach_example_fts_retrieval_returns_safe_records(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_outreach_example_document(_outreach_example_document("approved-example"))
    store.save_outreach_example_document(
        _outreach_example_document("unapproved-example", approved_for_drafting=False)
    )

    result = store.retrieve_outreach_examples(
        "clinical AI validation",
        company_type="trial technology company",
        opportunity_type="clinical AI validation",
        limit=3,
    )
    encoded_records = json.dumps(
        [record.model_dump(mode="json") for record in result.records],
        sort_keys=True,
    )

    assert [record.example_id for record in result.records] == ["approved-example"]
    assert result.records[0].raw_body_included is False
    assert result.records[0].match_reason
    assert "source_thread_id_hash" not in encoded_records
    assert '"raw_body":' not in encoded_records
    assert result.send_enabled is False


def test_all_schema_outputs_can_be_saved(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    email_id = store.save_email(_email_result())
    contact_id = store.save_contact(_contact_record())
    crm_context_id = store.save_crm_account_context(_crm_account_context())
    style_profile_id = store.save_email_style_profile(_email_style_profile())
    company_id = store.save_company(_company_profile())
    opportunity_id = store.save_opportunity(_opportunity_record())
    draft_id = store.save_outreach_draft(_outreach_draft())
    follow_up_id = store.save_follow_up_schedule(_follow_up_schedule())
    tracking_id = store.save_outreach_tracking(_outreach_tracking())
    approval_id = store.save_approval(
        object_type="outreach_draft",
        object_id=draft_id,
        decision="pending",
        scope="send",
        reviewer="fixture-reviewer",
        notes="Human review required.",
        risk_flags=["unsupported_claim"],
        source_agent="outreach_composer",
    )

    assert email_id == 1
    assert contact_id == 1
    assert crm_context_id == 1
    assert style_profile_id == 1
    assert company_id == 1
    assert opportunity_id == 1
    assert draft_id == 1
    assert follow_up_id == 1
    assert tracking_id == 1
    assert approval_id == 1
    assert store.count("sources") == 2
    assert store.count("contacts") == 1
    assert store.count("crm_contexts") == 1
    assert store.count("email_style_profiles") == 1
    assert store.count("follow_up_schedules") == 1
    assert store.count("outreach_tracking") == 1

    approval = store.fetch_all("approvals")[0]
    assert approval["object_type"] == "outreach_draft"
    assert approval["object_id"] == str(draft_id)
    assert approval["decision"] == "pending"
    assert approval["scope"] == "send"
    assert approval["reviewer"] == "fixture-reviewer"
    assert json.loads(approval["risk_flags_json"]) == ["unsupported_claim"]
    assert approval["source_agent"] == "outreach_composer"
    assert approval["timestamp"]
    assert approval["created_at_utc"].endswith("Z")
    assert approval["created_at_et"]
    assert approval["created_date_et"]


def test_follow_up_schedule_round_trip_is_data_only(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    row_id = store.save_follow_up_schedule(_follow_up_schedule())
    loaded = store.list_follow_up_schedules(company_name="NeuroFlow")[0]

    assert row_id == 1
    assert loaded.company_name == "NeuroFlow"
    assert loaded.related_draft_id == "1"
    assert loaded.proposed_date == "2026-05-01"
    assert loaded.approval_required is True
    assert loaded.send_enabled is False
    assert loaded.sent is False
    assert loaded.gmail_scheduled is False
    assert loaded.background_job_created is False


def test_outreach_tracking_round_trip_is_manual_lifecycle_only(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    row_id = store.save_outreach_tracking(_outreach_tracking())
    loaded = store.list_outreach_tracking(draft_id="1")[0]

    assert row_id == 1
    assert loaded.company_name == "NeuroFlow"
    assert loaded.lifecycle_status == "sent_manually"
    assert loaded.outreach_sent is True
    assert loaded.reply_received is False
    assert loaded.outcome == "pending_reply"
    assert loaded.manual_update_only is True
    assert loaded.send_enabled is False
    assert loaded.sent_by_agent is False


def test_initial_outreach_tracking_record_defaults_to_pending_approval() -> None:
    record = build_initial_outreach_tracking_record(
        draft_id=7,
        draft=_outreach_draft(),
    )

    assert record.draft_id == "7"
    assert record.company_name == "NeuroFlow"
    assert record.contact_name == "Dr. Example"
    assert record.lifecycle_status == "draft_pending_approval"
    assert record.outreach_sent is False
    assert record.reply_received is False
    assert record.outcome == "unknown"
    assert record.manual_update_only is True
    assert record.send_enabled is False
    assert record.sent_by_agent is False
    assert "manual only" in record.next_step


def test_outreach_tracking_rejects_send_or_agent_sent_flags() -> None:
    with pytest.raises(ValueError, match="must not enable sending"):
        OutreachTrackingRecord(
            draft_id="1",
            lifecycle_status="sent_manually",
            send_enabled=True,
        )

    with pytest.raises(ValueError, match="must not enable sending"):
        OutreachTrackingRecord(
            draft_id="1",
            lifecycle_status="sent_manually",
            sent_by_agent=True,
        )


def test_outreach_tracking_reply_normalizes_sent_state() -> None:
    record = OutreachTrackingRecord(
        draft_id="1",
        lifecycle_status="draft_created",
        reply_received=True,
        outcome="positive_reply",
        next_step="Prepare call notes.",
    )

    assert record.lifecycle_status == "reply_received"
    assert record.outreach_sent is True
    assert record.reply_received is True
    assert record.outcome == "positive_reply"


def test_company_features_round_trip_through_sqlite_profile_json(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    company_id = store.save_company(_company_profile())

    loaded_profile = store.load_company_profile(company_id)
    loaded_features = store.load_company_features(company_id)
    raw_profile = json.loads(store.fetch_all("companies")[0]["profile_json"])

    assert loaded_profile.features
    assert loaded_profile.source_backed_features
    assert loaded_features[0].feature_name == "market_segment"
    assert loaded_features[0].source_id == "fixture:neuroflow"
    assert raw_profile["features"][0]["evidence_text"] == ("Behavioral health technology company.")


def test_local_contact_and_crm_context_round_trip_from_storage(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.save_contact(_contact_record())
    store.save_crm_account_context(_crm_account_context())

    contacts = store.list_contacts(company_name="NeuroFlow", approved_only=True)
    contexts = store.list_crm_account_contexts(company_name="NeuroFlow", approved_only=True)

    assert contacts[0].contact_name == "Dr. Local Contact"
    assert contacts[0].approved_for_personalization is True
    assert contexts[0].account_stage == "researching"
    assert contexts[0].approved_for_personalization is True


def test_email_style_profile_round_trip_is_aggregate_and_approved_only(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_email_style_profile(_email_style_profile())
    store.save_email_style_profile(
        _email_style_profile().model_copy(
            update={
                "profile_id": "pending-style",
                "approval_state": ApprovalState.PENDING,
            }
        )
    )

    profiles = store.list_email_style_profiles(
        profile_id="neuroflow-style",
        approved_only=True,
    )
    payload = json.loads(load_email_style_profile("neuroflow-style", database_url=database_url))
    encoded = json.dumps(store.fetch_all("email_style_profiles"))

    assert profiles[0].profile_id == "neuroflow-style"
    assert profiles[0].approved_for_drafting is True
    assert payload["send_enabled"] is False
    assert payload["raw_sent_email_bodies_included"] is False
    assert payload["profile"]["preferred_phrases"] == ["compare notes"]
    assert "raw sent email body" not in encoded.lower()


def test_local_context_lookup_tools_return_approved_records_only(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_contact(_contact_record())
    store.save_contact(
        _contact_record().model_copy(
            update={
                "contact_name": "Dr. Pending Contact",
                "approval_state": ApprovalState.PENDING,
            }
        )
    )
    store.save_crm_account_context(_crm_account_context())

    contacts = json.loads(load_approved_contact_context("NeuroFlow", database_url=database_url))
    contexts = json.loads(load_approved_crm_context("NeuroFlow", database_url=database_url))

    assert contacts["send_enabled"] is False
    assert [contact["contact_name"] for contact in contacts["contacts"]] == ["Dr. Local Contact"]
    assert contexts["send_enabled"] is False
    assert contexts["contexts"][0]["account_stage"] == "researching"


def test_pending_approval_lookup_tool_returns_local_queue_context(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_approval_item(
        {
            "id": "approval-storage-tool",
            "object_type": "outreach_draft",
            "object_id": "draft-1",
            "title": "Review draft",
            "summary": "Review outbound draft.",
            "draft_text": "Draft only.",
            "source_agent": "outreach_composer",
        }
    )

    payload = json.loads(
        load_pending_approval_items(
            object_type="outreach_draft",
            source_agent="outreach_composer",
            database_url=database_url,
        )
    )

    assert payload["send_enabled"] is False
    assert [item["id"] for item in payload["items"]] == ["approval-storage-tool"]


def test_contact_storage_redacts_secrets_and_sensitive_personal_notes(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.save_contact(
        {
            "company": "NeuroFlow",
            "name": "Dr. Local Contact",
            "title": "Clinical Partnerships Lead",
            "source": "fixture",
            "confidence": 0.8,
            "approval_status": "approved_for_drafting",
            "notes": (
                "Patient Jane Doe was diagnosed with depression. token=SHOULD_NOT_APPEAR_123456789."
            ),
        }
    )

    contact = store.list_contacts(company_name="NeuroFlow")[0]
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "Patient Jane Doe" not in contact.notes
    assert "SHOULD_NOT_APPEAR" not in contact.notes
    assert "[REDACTED]" in contact.notes
    assert "Patient Jane Doe" not in dump
    assert "SHOULD_NOT_APPEAR" not in dump
    assert "[REDACTED]" in dump


def test_approval_state_machine_rejects_invalid_transitions() -> None:
    validate_approval_transition("pending", "approved_for_drafting")
    validate_approval_transition("approved_for_drafting", "approved_for_send")
    validate_approval_transition("approved_for_drafting", "approved_for_external_use")
    assert state_allows_drafting("approved_for_drafting") is True
    assert state_allows_external_use("approved_for_external_use") is True
    assert state_allows_external_use("approved_for_send") is True
    assert state_allows_sending("approved_for_drafting") is False
    assert state_allows_sending("approved_for_external_use") is False
    assert state_allows_sending("approved_for_send") is False

    with pytest.raises(ApprovalTransitionError):
        validate_approval_transition("approved_for_drafting", "approved_for_research")

    with pytest.raises(ApprovalTransitionError):
        validate_approval_transition("rejected", "approved_for_send")

    with pytest.raises(ApprovalTransitionError):
        validate_approval_transition("expired", "approved_for_drafting")


def test_rejected_and_expired_approvals_are_persisted_as_terminal(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    rejected_id = store.save_approval(
        object_type="opportunity",
        object_id="opp-1",
        decision=ApprovalState.REJECTED,
        scope=ApprovalScope.DRAFTING,
        reviewer="reviewer@example.com",
        notes="Not a fit.",
    )
    expired_id = store.save_approval(
        object_type="outreach_draft",
        object_id="draft-1",
        decision=ApprovalState.EXPIRED,
        scope=ApprovalScope.SEND,
        reviewer="reviewer@example.com",
        notes="Timed out.",
    )

    rows = store.fetch_all("approvals")
    assert rejected_id == 1
    assert expired_id == 2
    assert [row["decision"] for row in rows] == ["rejected", "expired"]
    assert [row["scope"] for row in rows] == ["drafting", "send"]


def test_approval_decision_timestamp_has_eastern_metadata(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.save_approval(
        object_type="outreach_draft",
        object_id="draft-2",
        decision=ApprovalState.APPROVED_FOR_SEND,
        scope=ApprovalScope.SEND,
        timestamp="2026-04-21T03:30:00Z",
    )

    approval = store.fetch_all("approvals")[0]
    assert approval["timestamp"] == "2026-04-21T03:30:00Z"
    assert approval["decision_at_et"].startswith("2026-04-20T23:30:00")
    assert approval["decision_date_et"] == "2026-04-20"


def test_approval_records_redact_source_agent_notes_and_risk_flags(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.save_approval(
        object_type="outreach_draft",
        object_id="draft-secret",
        decision="pending",
        scope="send",
        reviewer="reviewer@example.com",
        notes="token=SHOULD_NOT_APPEAR_123456789",
        risk_flags=["authorization=Bearer SHOULD_NOT_APPEAR_222222222222"],
        source_agent="agent-secret=SHOULD_NOT_APPEAR_333333333",
    )

    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "SHOULD_NOT_APPEAR" not in dump
    assert "[REDACTED]" in dump


def test_fetch_by_created_date_et_filters_rows(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    first_id = store.save_agent_run(agent_name="first", input_summary="first")
    second_id = store.save_agent_run(agent_name="second", input_summary="second")

    rows = store.fetch_all("agent_runs")
    created_date_et = rows[0]["created_date_et"]
    filtered = store.fetch_by_created_date_et("agent_runs", created_date_et)

    assert [row["id"] for row in filtered] == [first_id, second_id]
    assert store.fetch_by_created_date_et("agent_runs", "1900-01-01") == []


def test_storage_tool_filters_by_created_date_et(tmp_path) -> None:
    tool = StorageTool(_database_url(tmp_path))
    tool.save_agent_run(agent_name="orchestrator", input_summary="route")
    created_date_et = tool.list_records("agent_runs")[0]["created_date_et"]

    rows = tool.list_records_by_created_date_et("agent_runs", created_date_et)

    assert len(rows) == 1


def test_storage_tool_records_contextual_tool_events(tmp_path) -> None:
    tool = StorageTool(
        _database_url(tmp_path),
        agent_name="keystone_pipeline",
        run_id="pipeline-run-1",
        dry_run=True,
    )

    tool.save_email(
        {
            **_email_result().model_dump(mode="json"),
            "body": "Full inbound body should be hashed only.",
        }
    )

    events = tool.list_tool_events(agent_name="keystone_pipeline", run_id="pipeline-run-1")

    assert len(events) == 1
    assert events[0]["tool_name"] == "storage_save_email"
    assert events[0]["dry_run"] == 1
    assert events[0]["status"] == "success"
    assert "body_hash" in events[0]["input_summary"]
    assert "Full inbound body" not in events[0]["input_summary"]


def test_storage_tool_wraps_outreach_tracking(tmp_path) -> None:
    tool = StorageTool(
        _database_url(tmp_path),
        agent_name="outreach_composer",
        run_id="tracking-run-1",
    )

    result = tool.save_outreach_tracking(_outreach_tracking())
    records = tool.list_outreach_tracking(draft_id="1")
    events = tool.list_tool_events(agent_name="outreach_composer", run_id="tracking-run-1")

    assert result["table"] == "outreach_tracking"
    assert records[0]["lifecycle_status"] == "sent_manually"
    assert records[0]["send_enabled"] is False
    assert events[0]["tool_name"] == "storage_save_outreach_tracking"


def test_storage_tool_saves_initial_outreach_tracking_for_saved_draft(tmp_path) -> None:
    tool = StorageTool(
        _database_url(tmp_path),
        agent_name="outreach_composer",
        run_id="draft-run-1",
    )
    draft = _outreach_draft()
    saved_draft = tool.save_outreach_draft(draft)

    result = tool.save_initial_outreach_tracking(
        draft_id=saved_draft["id"],
        draft=draft,
    )
    records = tool.list_outreach_tracking(draft_id=saved_draft["id"])
    events = tool.list_tool_events(agent_name="outreach_composer", run_id="draft-run-1")

    assert result["table"] == "outreach_tracking"
    assert records[0]["lifecycle_status"] == "draft_pending_approval"
    assert records[0]["outreach_sent"] is False
    assert records[0]["reply_received"] is False
    assert records[0]["outcome"] == "unknown"
    assert records[0]["send_enabled"] is False
    assert records[0]["sent_by_agent"] is False
    assert {event["tool_name"] for event in events} == {
        "storage_save_outreach_draft",
        "storage_save_initial_outreach_tracking",
    }


def test_storage_tool_records_blocked_tool_event(tmp_path) -> None:
    tool = StorageTool(_database_url(tmp_path), agent_name="gmail_triage", run_id="run-err")

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        tool.save(
            {"body": ("Patient Jane Doe was diagnosed with depression and should not be logged.")}
        )

    events = tool.list_tool_events(status="error")
    event = events[0]
    with sqlite3.connect(tool.store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert len(events) == 1
    assert event["tool_name"] == "storage_save"
    assert event["agent_name"] == "gmail_triage"
    assert event["run_id"] == "run-err"
    assert "ToolGuardrailViolation" in event["output_summary"]
    assert "possible PHI" in event["error"]
    assert "Patient Jane Doe" not in dump


def test_agent_run_logs_save(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    row_id = store.save_agent_run(
        agent_name="orchestrator",
        input_payload={"input": "find behavioral health AI companies"},
        input_summary="find behavioral health AI companies",
        output={"route": "opportunity_scout"},
        model="fixture",
        dry_run=True,
        status="success",
    )

    row = store.fetch_all("agent_runs")[0]
    assert row_id == 1
    assert row["agent_name"] == "orchestrator"
    assert row["input_hash"]
    assert json.loads(row["output_json"])["route"] == "opportunity_scout"
    assert row["dry_run"] == 1


def test_agent_run_actual_cost_annotation_updates_sdk_cost(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    row_id = store.save_agent_run(
        agent_name="orchestrator",
        input_summary="cache experiment",
        output={
            "_sdk_cost": {
                "source": "local_pricing_table",
                "estimated_usd": 0.16,
            },
            "_sdk_usage": {
                "input_tokens": 10_000,
                "cached_input_tokens": 5_000,
            },
        },
        model="sdk-live",
        dry_run=False,
    )

    result = store.annotate_agent_run_actual_cost(
        row_id,
        actual_usd="0.15",
        reference_id="platform-window-1",
    )

    output = json.loads(store.fetch_all("agent_runs")[0]["output_json"])
    comparison = output["_sdk_cost"]["estimate_vs_actual"]
    assert result["status"] == "updated"
    assert output["_sdk_cost"]["actual_usd"] == 0.15
    assert comparison["available"] is True
    assert comparison["reference_id"] == "platform-window-1"
    assert comparison["delta_usd"] == 0.01


def test_agent_run_step_logs_save_and_query_redacted_summaries(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    row_id = store.save_agent_run_log(
        run_id="pipeline-run-1",
        step_name="gmail_triage",
        agent_name="gmail_triage",
        input_payload={
            "subject": "Consulting support",
            "body": "Full inbound email body SHOULD_NOT_APPEAR_BODY.",
            "api_key": "SHOULD_NOT_APPEAR_SECRET_123456789",
        },
        output={
            "category": "consulting_opportunity",
            "draft_reply": "Draft reply SHOULD_NOT_APPEAR_DRAFT.",
        },
        dry_run=True,
        status="success",
        timestamp="2026-04-21T03:30:00Z",
    )

    logs = store.list_agent_run_logs(run_id="pipeline-run-1")
    log = logs[0]
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert row_id == 1
    assert len(logs) == 1
    assert log["step_name"] == "gmail_triage"
    assert log["agent_name"] == "gmail_triage"
    assert log["run_id"] == "pipeline-run-1"
    assert log["input_hash"]
    assert log["output_hash"]
    assert log["dry_run"] == 1
    assert log["status"] == "success"
    assert log["created_at_utc"] == "2026-04-21T03:30:00Z"
    assert "body_hash" in log["input_summary"]
    assert "draft_reply_hash" in log["output_summary"]
    assert "SHOULD_NOT_APPEAR" not in dump
    assert "Full inbound email body" not in dump


def test_storage_audit_hashes_body_like_fields_and_redacts_secret_strings(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    secret = "SHOULD_NOT_APPEAR_SECRET_123456789"

    store.save_agent_run_log(
        run_id="hygiene-run-1",
        step_name="credential_hygiene",
        agent_name="regression",
        input_payload={
            "content": f"Full sensitive content with token={secret}.",
            "authorization": f"Bearer {secret}",
        },
        output={
            "full_text": f"Full output body with api_key={secret}.",
            "draft_text": f"Draft body with password={secret}.",
        },
        dry_run=True,
        status="success",
    )

    log = store.list_agent_run_logs(run_id="hygiene-run-1")[0]
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "content_hash" in log["input_summary"]
    assert "full_text_hash" in log["output_summary"]
    assert "draft_text_hash" in log["output_summary"]
    assert "Full sensitive content" not in dump
    assert "Full output body" not in dump
    assert "Draft body" not in dump
    assert secret not in dump
    assert "SHOULD_NOT_APPEAR" not in dump
    assert "[REDACTED]" in dump


def test_failed_agent_run_step_log_redacts_error_and_payload(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    store.save_agent_run_log(
        run_id="pipeline-run-error",
        step_name="account_research",
        agent_name="business_research_analyst",
        input_payload={
            "company": "FixtureCo",
            "body": "Patient Jane Doe was diagnosed with depression.",
        },
        output={"error_type": "RuntimeError"},
        dry_run=True,
        status="error",
        error="token=SHOULD_NOT_APPEAR_123456789 failed during source fetch",
    )

    log = store.list_agent_run_logs(status="error")[0]
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert log["status"] == "error"
    assert log["error"]
    assert "SHOULD_NOT_APPEAR" not in log["error"]
    assert "Patient Jane Doe" not in dump
    assert "SHOULD_NOT_APPEAR" not in dump


def test_orchestrator_result_saves_as_agent_run(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    row_id = store.save_orchestrator_result(
        OrchestratorResult(route="opportunity_scout", rationale="Fixture route."),
        input_payload={"input": "find leads"},
        input_summary="find leads",
    )

    row = store.fetch_all("agent_runs")[0]
    assert row_id == 1
    assert row["agent_name"] == "orchestrator"
    assert json.loads(row["output_json"])["route"] == "opportunity_scout"


def test_tool_events_save_and_query_redacted_summaries(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    row_id = store.save_tool_event(
        tool_name="gmail_create_draft_reply",
        agent_name="gmail_triage",
        run_id="run-123",
        input_payload={
            "message_id": "msg-1",
            "body": "Full sensitive email body SHOULD_NOT_APPEAR_BODY.",
            "api_key": "SHOULD_NOT_APPEAR_SECRET_123456789",
        },
        output={
            "status": "dry-run",
            "draft_body": "Draft body SHOULD_NOT_APPEAR_DRAFT.",
        },
        dry_run=True,
        status="success",
        timestamp="2026-04-21T03:30:00Z",
    )

    events = store.list_tool_events(run_id="run-123")
    event = events[0]
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert row_id == 1
    assert len(events) == 1
    assert event["tool_name"] == "gmail_create_draft_reply"
    assert event["agent_name"] == "gmail_triage"
    assert event["run_id"] == "run-123"
    assert event["input_hash"]
    assert event["output_hash"]
    assert event["dry_run"] == 1
    assert event["status"] == "success"
    assert event["created_at_utc"] == "2026-04-21T03:30:00Z"
    assert "body_hash" in event["input_summary"]
    assert "draft_body_hash" in event["output_summary"]
    assert "SHOULD_NOT_APPEAR" not in dump
    assert "Full sensitive email body" not in dump
    assert "[REDACTED]" in dump


def test_no_secrets_are_saved(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_agent_run(
        agent_name="secret_test",
        input_payload={"OPENAI_API_KEY": "SENTINEL_VALUE_123456789"},
        input_summary="api_key=SHOULD_NOT_APPEAR_123456789",
        output={
            "token": "SHOULD_NOT_APPEAR_987654321",
            "message": "safe",
            "authorization": "Bearer SHOULD_NOT_APPEAR_222222222222",
        },
        model="fixture",
        dry_run=True,
        status="success",
        error="password=SHOULD_NOT_APPEAR_555555555",
    )
    store.save_source(
        object_type="company",
        object_id=1,
        title="Source",
        url="fixture://source",
        snippet="secret=SHOULD_NOT_APPEAR_000000000",
    )

    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "SHOULD_NOT_APPEAR" not in dump
    assert "SENTINEL_VALUE" not in dump
    assert "[REDACTED]" in dump


def test_agent_run_output_summarizes_email_and_draft_bodies(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_agent_run(
        agent_name="body_test",
        input_summary="body handling",
        output={
            "draft_reply": "Draft reply should not be stored in full.",
            "email_body": "Generated outreach body should not be duplicated in run logs.",
        },
    )

    output = json.loads(store.fetch_all("agent_runs")[0]["output_json"])

    assert "draft_reply" not in output
    assert "email_body" not in output
    assert output["draft_reply_hash"]
    assert output["draft_reply_summary"] == "Draft reply should not be stored in full."
    assert output["email_body_hash"]
    assert output["email_body_summary"] == (
        "Generated outreach body should not be duplicated in run logs."
    )


def test_email_storage_summarizes_body_and_draft_reply(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_email(
        {
            **_email_result().model_dump(mode="json"),
            "body": "Full inbound body with secret=SHOULD_NOT_APPEAR_111111111.",
            "draft_reply": "Draft response with token=SHOULD_NOT_APPEAR_222222222.",
        }
    )

    triage_json = json.loads(store.fetch_all("emails")[0]["triage_json"])
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "body" not in triage_json
    assert "draft_reply" not in triage_json
    assert triage_json["body_hash"]
    assert triage_json["draft_reply_hash"]
    assert "body_summary" in triage_json
    assert "draft_reply_preview" in triage_json
    assert "SHOULD_NOT_APPEAR" not in dump


def test_outreach_draft_stores_redacted_full_body_for_approval(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_outreach_draft(
        {
            "company_name": "NeuroFlow",
            "contact_name": "Dr. Example",
            "email_subject": "Clinical AI workflow discussion",
            "email_body": (
                "Hello, I noticed the public workflow work. "
                "token=SHOULD_NOT_APPEAR_333333333. Could we compare notes?"
            ),
            "personalization_rationale": "Uses approved public context.",
            "approval_state": "pending",
        }
    )

    row = store.fetch_all("outreach_drafts")[0]
    draft_json = json.loads(row["draft_json"])
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert "approval artifact" in OUTREACH_EMAIL_BODY_STORAGE_NOTE
    assert "Could we compare notes?" in row["email_body"]
    assert "[REDACTED]" in row["email_body"]
    assert "email_body" not in draft_json
    assert draft_json["email_body_hash"]
    assert "email_body_summary" in draft_json
    assert "SHOULD_NOT_APPEAR" not in dump


def test_storage_tool_wraps_sqlite_store(tmp_path) -> None:
    tool = StorageTool(_database_url(tmp_path))

    init_result = tool.init_db()
    save_result = tool.save_email(_email_result())

    assert init_result["status"] == "initialized"
    assert init_result["schema_version"] == CURRENT_SCHEMA_VERSION
    assert init_result["migrations"] == _expected_migration_versions()
    assert save_result["table"] == "emails"
    assert len(tool.list_records("emails")) == 1
    assert [event["tool_name"] for event in tool.list_tool_events()] == [
        "storage_init_db",
        "storage_save_email",
    ]


def test_redact_email_content_summarizes_full_body() -> None:
    payload = redact_secrets(
        {
            "body": "line one\nline two",
            "draft_reply": "draft line one\nline two",
            "api_key": "SHOULD_NOT_APPEAR_123456789",
        },
        summarize_email_content=True,
    )

    assert "body_hash" in payload
    assert "draft_reply_hash" in payload
    assert "draft_reply" not in payload
    assert payload["api_key"] == "[REDACTED]"
    assert "line one" in payload["body_summary"]
    assert "draft line one" in payload["draft_reply_summary"]


def test_redact_secrets_preserves_sdk_usage_token_counts() -> None:
    payload = redact_secrets(
        {
            "_sdk_usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 900,
                "output_tokens": 300,
                "reasoning_output_tokens": 40,
                "total_tokens": 1540,
                "cache_hit_rate": 0.75,
                "prompt_cache_key_present": True,
                "prompt_cache_key_hash": "abc123def456",
            },
            "api_token": "SHOULD_NOT_APPEAR_123456789",
        }
    )

    assert payload["_sdk_usage"]["input_tokens"] == 1200
    assert payload["_sdk_usage"]["cached_input_tokens"] == 900
    assert payload["_sdk_usage"]["reasoning_output_tokens"] == 40
    assert payload["_sdk_usage"]["cache_hit_rate"] == 0.75
    assert payload["_sdk_usage"]["prompt_cache_key_hash"] == "abc123def456"
    assert payload["api_token"] == "[REDACTED]"
