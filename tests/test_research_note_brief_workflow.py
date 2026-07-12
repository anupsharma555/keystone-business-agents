from __future__ import annotations

import pytest

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.research_note_brief_workflow import (
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    SelectedResearchNote,
    execute_research_note_brief_workflow,
    research_note_brief_privacy_preview,
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
        text=(
            "A bounded internal research note about evaluation design, evidence quality, "
            "and agent workflow. The next step recommends a reviewed source checklist."
        ),
    )


def _brief(source_id: str) -> ResearchBrief:
    return ResearchBrief(
        target_name="Selected internal research note",
        target_type="topic",
        research_goal="Prepare a one-page internal brief.",
        summary=(
            "The typed assertions connect research operations, agent workflow, and "
            "source provenance to a reviewed next step. Raw source text was not provided."
        ),
        key_findings=[
            "Evaluation design and evidence quality are present alongside research operations."
        ],
        facts=[
            ResearchBriefFact(
                text="The note discusses evaluation design.",
                source_ids=[source_id],
                confidence=0.95,
            )
        ],
        inferences=["KNI can turn this into a reusable review checklist."],
        limitations=[
            "No explicit limitation signal was observed in the abstracted assertion packet."
        ],
        next_steps=["Review the proposed checklist before operational use."],
        sources=[
            ResearchSourceCitation(
                source_id=source_id,
                title=source_id,
                url=f"urn:sha256:{source_id.removeprefix('sanitized-source:')}",
                source_type="privacy_minimized_signal",
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
    assert result["plan"]["proof_scope"] == "sanitized_context_proof"
    assert _note().text not in str(result)


def test_privacy_preview_omits_note_identity_and_body() -> None:
    note = _note()

    result = research_note_brief_privacy_preview(note)

    assert result["status"] == "privacy_minimized_preview"
    assert result["local_source_mapping_verified"] is True
    assert result["openai_requests_made"] == 0
    assert result["provider_writes"] == 0
    assert result["signal_sufficient"] is True
    assert note.document_id not in str(result)
    assert note.title not in str(result)
    assert note.text not in str(result)
    context = result["bundle"]["privacy_minimized_context"]
    assert context["proof_scope"] == "sanitized_context_proof"
    assert context["transmission_contract"]["raw_text"] is False
    assertions = {(item["predicate"], item["object"]) for item in context["assertions"]}
    assert ("contains", "research_operations") in assertions
    assert ("next_action_status", "present") in assertions
    assert ("limitation_status", "not_observed") in assertions


def test_privacy_preview_rejects_sparse_context_without_inventing_findings() -> None:
    note = SelectedResearchNote(
        document_id="doc-sparse",
        title="Sparse note",
        modified_time="2026-07-10T12:00:00Z",
        text="One next step is listed.",
    )

    result = research_note_brief_privacy_preview(note)

    assert result["status"] == "privacy_minimized_preview_insufficient"
    assert result["signal_count"] == 1
    assert result["signal_sufficient"] is False
    assertions = result["bundle"]["privacy_minimized_context"]["assertions"]
    assert {(item["predicate"], item["object"]) for item in assertions} >= {
        ("contains", "next_steps_present"),
        ("evidence_density", "sparse"),
        ("limitation_status", "not_observed"),
        ("next_action_status", "present"),
    }
    assert result["openai_requests_made"] == 0


def test_trusted_private_mode_is_disabled() -> None:
    with pytest.raises(ValueError, match="Trusted-private research-note synthesis is disabled"):
        execute_research_note_brief_workflow(
            note=_note(),
            suffix="trusted",
            approval_reference="approval",
            live_sdk=True,
            live_workspace_writes=False,
            approved_private_context=True,
        )


def test_joined_fake_model_preserves_identity_and_invokes_disposable_lifecycle(
    monkeypatch,
) -> None:
    note = _note()
    captured = {}

    def fake_runner(typed_input, **kwargs):
        captured["prompt"] = typed_input.to_prompt()
        captured["typed_input"] = typed_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="business_research_analyst",
            output=_brief(typed_input.local_context_source_ids[0]),
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
        approved_privacy_minimized_context=True,
        sdk_runner=fake_runner,
    )

    assert result["status"] == "passed"
    assert result["source_identity_preserved"] is True
    assert result["lifecycle"]["same_document_identity"] is True
    assert captured["kwargs"] == {"live": True, "attach_tools": False, "max_turns": 1}
    assert note.source_id not in captured["prompt"]
    assert note.title not in captured["prompt"]
    assert note.text not in captured["prompt"]
    assert "sanitized-context" not in captured["prompt"]
    assert (
        "No explicit limitation was observed in the typed assertion layer."
        in captured["prompt"]
    )
    assert (
        "No explicit limitation was observed in the typed assertion layer."
        in captured["payload"]["output"]["limitations"]
    )
    assert "sanitized-source:" in captured["prompt"]
    assert captured["payload"]["output_type"] == "ResearchBrief"
    normalized_source = captured["payload"]["output"]["sources"][0]
    assert normalized_source["title"] == "Sanitized internal research assertion source"
    assert normalized_source["url"].startswith("urn:sha256:")
    assert captured["lifecycle_kwargs"]["live"] is True
    assert result["data_handling"]["response_store"] is False


def test_joined_workflow_rejects_generic_brief_that_ignores_assertions() -> None:
    note = _note()

    def fake_runner(typed_input, **_kwargs):
        generic = _brief(typed_input.local_context_source_ids[0]).model_copy(
            update={
                "summary": "A generic internal brief was prepared.",
                "key_findings": ["Review the source later."],
                "inferences": [],
                "limitations": ["Internal review is required."],
            }
        )
        return TypedAgentRunResult(
            agent_name="business_research_analyst",
            output=generic,
            raw_result={},
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.02},
        )

    with pytest.raises(RuntimeError, match="typed assertion categories"):
        execute_research_note_brief_workflow(
            note=note,
            suffix="generic",
            approval_reference="approval",
            live_sdk=True,
            live_workspace_writes=False,
            approved_privacy_minimized_context=True,
            sdk_runner=fake_runner,
        )


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
        assert "typed privacy-minimized assertion packet" in str(exc)
    else:
        raise AssertionError("Expected private-context approval failure")
