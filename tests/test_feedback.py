from __future__ import annotations

import json

import pytest

import scripts.respond_feedback_request as respond_feedback_cli
from keystone_agents.feedback import (
    build_operator_feedback_request,
    export_feedback_to_markdown,
    list_feedback,
    respond_feedback_request,
    save_feedback,
    summarize_feedback_by_tag,
)
from keystone_agents.reporting import render_operator_feedback_question
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.feedback import (
    OUTREACH_REVIEW_FEEDBACK_TAGS,
    SUGGESTED_FEEDBACK_TAGS,
    FeedbackRecord,
    FeedbackResponseResult,
    OperatorFeedbackRequest,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'feedback.db'}"


def _approval_item(
    *,
    item_id: str = "approval-feedback-1",
    object_id: str = "Curebase",
    metadata: dict[str, object] | None = None,
) -> ApprovalQueueItem:
    return ApprovalQueueItem(
        id=item_id,
        object_type="outreach_draft",
        object_id=object_id,
        title="Curebase outreach draft",
        summary="Draft review for Curebase outreach.",
        draft_text="Subject: Quick intro\n\nWould welcome a short call next week.",
        source_agent="outreach_composer",
        approval_status="pending",
        metadata=metadata or {},
    )


def test_feedback_saved(tmp_path) -> None:
    record = save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="poor",
        tags=["too_salesy", "weak_sourcing"],
        notes="Tone was too generic and sourcing was thin.",
        database_url=_database_url(tmp_path),
    )

    assert isinstance(record, FeedbackRecord)
    assert record.id == 1
    assert record.object_type == "outreach_draft"
    assert record.object_id == "draft-1"
    assert record.rating == "poor"
    assert record.tags == ["too_salesy", "weak_sourcing"]
    assert record.created_at.endswith("Z")


def test_feedback_listed_with_filters(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    save_feedback(
        object_type="company_profile",
        object_id="company-1",
        rating="good",
        tags=["good_fit"],
        notes="Strong target.",
        database_url=database_url,
    )
    save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="okay",
        tags=["too_generic", "needs_more_context"],
        notes="Usable but thin.",
        database_url=database_url,
    )

    all_records = list_feedback(database_url=database_url)
    outreach_records = list_feedback(object_type="outreach_draft", database_url=database_url)
    tagged_records = list_feedback(tag="needs_more_context", database_url=database_url)
    okay_records = list_feedback(rating="okay", database_url=database_url)

    assert len(all_records) == 2
    assert [record.object_id for record in outreach_records] == ["draft-1"]
    assert [record.object_id for record in tagged_records] == ["draft-1"]
    assert [record.rating for record in okay_records] == ["okay"]


def test_feedback_tags_summarized(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="poor",
        tags=["weak_sourcing", "too_generic"],
        database_url=database_url,
    )
    save_feedback(
        object_type="opportunity",
        object_id="opp-1",
        rating="okay",
        tags=["weak_sourcing"],
        database_url=database_url,
    )

    summary = summarize_feedback_by_tag(database_url=database_url)

    assert summary == {"weak_sourcing": 2, "too_generic": 1}


def test_feedback_markdown_export_works(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="good",
        tags=["excellent_personalization"],
        notes="Personalization was specific and well sourced.",
        database_url=database_url,
    )

    markdown = export_feedback_to_markdown(database_url=database_url)

    assert "# Keystone Feedback Export" in markdown
    assert "Records: 1" in markdown
    assert "excellent_personalization: 1" in markdown
    assert "Personalization was specific and well sourced." in markdown


def test_feedback_storage_uses_local_sqlite_without_live_apis(tmp_path, monkeypatch) -> None:
    for name in (
        "KEYSTONE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "SERPER_API_KEY",
        "SLACK_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    store = SQLiteStore(_database_url(tmp_path))
    row_id = store.save_feedback(
        object_type="email_triage",
        object_id="msg-1",
        rating="okay",
        tags=["needs_more_context"],
        notes="Needs more sender context.",
    )

    assert row_id == 1
    assert "feedback" in store.table_names()
    assert store.list_feedback()[0]["tags"] == ["needs_more_context"]
    assert "weak_sourcing" in SUGGESTED_FEEDBACK_TAGS


def test_operator_feedback_request_uses_outreach_review_tags() -> None:
    request = build_operator_feedback_request(
        object_type="outreach_draft",
        object_id="draft-1",
        source_agent="outreach_composer",
    )

    assert isinstance(request, OperatorFeedbackRequest)
    assert request.object_type == "outreach_draft"
    assert request.optional is True
    assert request.send_enabled is False
    assert {"too_generic", "weak_personalization", "good_cta"} <= set(request.suggested_tags)
    assert set(OUTREACH_REVIEW_FEEDBACK_TAGS) <= set(request.suggested_tags)
    assert request.capture_fields.rating == "good|okay|poor"


def test_operator_feedback_request_renders_as_human_question() -> None:
    request = build_operator_feedback_request(
        object_type="outreach_draft",
        object_id="draft-1",
        source_agent="outreach_composer",
    )

    rendered = render_operator_feedback_question(request)

    assert rendered.startswith("Question:")
    assert "Should this artifact be approved, revised, or rejected?" in rendered
    assert "Rate good, okay, poor" in rendered
    assert "too_generic" in rendered
    assert "weak_personalization" in rendered


def test_feedback_tag_normalization_accepts_operator_phrases(tmp_path) -> None:
    record = save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="okay",
        tags=["Worth sending after light edit", "unsupported-claim-risk"],
        database_url=_database_url(tmp_path),
    )

    assert record.tags == [
        "worth_sending_after_light_edit",
        "unsupported_claim_risk",
    ]


def test_approval_linked_feedback_response_saves_feedback_and_memory(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    request = build_operator_feedback_request(
        object_type="outreach_draft",
        object_id="Curebase",
        source_agent="outreach_composer",
    )
    store.save_approval_item(
        _approval_item(metadata={"operator_feedback_request": request.model_dump(mode="json")})
    )

    result = respond_feedback_request(
        approval_id="approval-feedback-1",
        rating="okay",
        tags=["good tone", "worth sending after light edit"],
        notes="Tighten the opening and keep the CTA specific.",
        reviewer="anup",
        database_url=database_url,
    )

    assert isinstance(result, FeedbackResponseResult)
    assert result.approval_id == "approval-feedback-1"
    assert result.feedback.approval_id == "approval-feedback-1"
    assert result.feedback.source_agent == "outreach_composer"
    assert result.feedback.review_stage == "approval_review"
    assert result.memory_id == 1
    assert result.object_key == "curebase"

    linked_records = list_feedback(approval_id="approval-feedback-1", database_url=database_url)
    assert [record.id for record in linked_records] == [result.feedback.id]

    refreshed = store.get_approval_item("approval-feedback-1")
    assert refreshed is not None
    assert refreshed.metadata["operator_feedback_response_count"] == 1
    assert refreshed.metadata["latest_feedback_id"] == result.feedback.id
    assert refreshed.metadata["latest_feedback_rating"] == "okay"
    assert refreshed.metadata["latest_feedback_memory_id"] == 1

    memory_records = store.retrieve_memory(
        "cta",
        object_key="Curebase",
        memory_types=["human_feedback"],
    )
    assert len(memory_records) == 1
    assert memory_records[0].content["notes_summary"] == (
        "Tighten the opening and keep the CTA specific."
    )
    assert memory_records[0].content["tags"] == [
        "good_tone",
        "worth_sending_after_light_edit",
    ]


def test_approval_linked_feedback_response_requires_attached_request(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_approval_item(_approval_item(metadata={}))

    with pytest.raises(ValueError, match="operator_feedback_request"):
        respond_feedback_request(
            approval_id="approval-feedback-1",
            rating="poor",
            notes="Not usable.",
            database_url=database_url,
        )


def test_respond_feedback_request_cli_outputs_linked_result(tmp_path, capsys) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    request = build_operator_feedback_request(
        object_type="outreach_draft",
        object_id="Curebase",
        source_agent="outreach_composer",
    )
    store.save_approval_item(
        _approval_item(
            item_id="approval-feedback-cli",
            metadata={"operator_feedback_request": request.model_dump(mode="json")},
        )
    )

    assert (
        respond_feedback_cli.main(
            [
                "--approval-id",
                "approval-feedback-cli",
                "--rating",
                "good",
                "--tag",
                "good CTA",
                "--notes",
                "Strong opening and useful CTA.",
                "--database-url",
                database_url,
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["approval_id"] == "approval-feedback-cli"
    assert payload["feedback"]["approval_id"] == "approval-feedback-cli"
    assert payload["feedback"]["tags"] == ["good_cta"]
    assert payload["memory_id"] == 1
