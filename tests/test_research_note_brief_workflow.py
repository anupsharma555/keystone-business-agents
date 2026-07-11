from __future__ import annotations

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.research_note_brief_workflow import (
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    SelectedResearchNote,
    execute_research_note_brief_workflow,
    resolve_latest_research_note,
)
from keystone_agents.schemas.research import (
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)


def _note() -> SelectedResearchNote:
    return SelectedResearchNote(
        document_id="doc-selected",
        title="Clinical AI research note",
        modified_time="2026-07-10T12:00:00Z",
        text="A bounded internal note about evaluation design and evidence quality.",
    )


def _brief(note: SelectedResearchNote) -> ResearchBrief:
    return ResearchBrief(
        target_name=note.title,
        target_type="topic",
        research_goal="Prepare a one-page internal brief.",
        summary="The note recommends explicit evaluation design and evidence-quality checks.",
        key_findings=["Evaluation design should be explicit."],
        facts=[
            ResearchBriefFact(
                text="The note discusses evaluation design.",
                source_ids=[note.source_id],
                confidence=0.95,
            )
        ],
        inferences=["KNI can turn this into a reusable review checklist."],
        limitations=["The note is an internal source and requires review."],
        next_steps=["Review the proposed checklist before operational use."],
        sources=[
            ResearchSourceCitation(
                source_id=note.source_id,
                title=note.title,
                url=note.source_url,
                source_type="google_workspace_document",
            )
        ],
    )


def test_resolver_keeps_unique_latest_body_in_process() -> None:
    note = resolve_latest_research_note(
        live=True,
        search_files=lambda *_args, **_kwargs: {
            "items": [{"id": "folder", "name": "Research", "mime_type": GOOGLE_FOLDER_MIME}]
        },
        list_folder=lambda *_args, **_kwargs: {
            "items": [
                {
                    "id": "old",
                    "name": "Old",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-07-01T00:00:00Z",
                },
                {
                    "id": "doc-selected",
                    "name": "Latest",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-07-10T12:00:00Z",
                },
            ]
        },
        read_doc=lambda document_id, **_kwargs: {
            "status": "success",
            "document_id": document_id,
            "title": "Clinical AI research note",
            "text": "Bounded note body",
            "truncated": False,
        },
    )

    assert note.document_id == "doc-selected"
    assert note.text == "Bounded note body"


def test_offline_workflow_persists_hashes_not_note_body() -> None:
    result = execute_research_note_brief_workflow(
        note=_note(),
        suffix="abc",
        approval_reference="approval",
        live_sdk=False,
        live_workspace_writes=False,
    )

    assert result["status"] == "validated_offline"
    assert result["plan"]["source_body_persisted"] is False
    assert result["plan"]["max_openai_requests"] == 1
    assert _note().text not in str(result)


def test_joined_fake_model_preserves_identity_and_invokes_disposable_lifecycle(
    monkeypatch,
) -> None:
    note = _note()
    captured = {}

    def fake_runner(typed_input, **kwargs):
        captured["prompt"] = typed_input.to_prompt()
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="business_research_analyst",
            output=_brief(note),
            raw_result={},
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.02},
        )

    def fake_lifecycle(payload, **kwargs):
        captured["payload"] = payload
        captured["lifecycle_kwargs"] = kwargs
        return {
            "status": "passed",
            "same_document_identity": True,
            "receipts": {
                "create_readback": {"passed": True},
                "update_readback": {"passed": True},
                "trash_readback": {"passed": True},
            },
            "send_enabled": False,
        }

    monkeypatch.setattr(
        "keystone_agents.research_note_brief_workflow.execute_research_doc_lifecycle",
        fake_lifecycle,
    )
    result = execute_research_note_brief_workflow(
        note=note,
        suffix="abc",
        approval_reference="approval",
        live_sdk=True,
        live_workspace_writes=True,
        approved_private_context=True,
        sdk_runner=fake_runner,
    )

    assert result["status"] == "passed"
    assert result["source_identity_preserved"] is True
    assert result["lifecycle"]["same_document_identity"] is True
    assert captured["kwargs"] == {"live": True, "attach_tools": False, "max_turns": 1}
    assert note.source_id in captured["prompt"]
    assert captured["payload"]["output_type"] == "ResearchBrief"
    assert captured["lifecycle_kwargs"]["live"] is True
    assert result["data_handling"]["response_store"] is False


def test_joined_workflow_requires_private_context_approval() -> None:
    try:
        execute_research_note_brief_workflow(
            note=_note(),
            suffix="abc",
            approval_reference="approval",
            live_sdk=True,
            live_workspace_writes=False,
        )
    except ValueError as exc:
        assert "private-context approval" in str(exc)
    else:
        raise AssertionError("Expected private-context approval failure")
