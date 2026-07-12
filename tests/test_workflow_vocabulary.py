from __future__ import annotations

import pytest

from keystone_agents.workflow_vocabulary import (
    ControlledTagSet,
    EvidenceTag,
    ObjectTag,
    SafetyTag,
    StorageTag,
    WorkflowTag,
    attach_controlled_tags,
    normalize_controlled_tag,
)


def test_controlled_tags_cover_cross_system_context_dimensions() -> None:
    tags = ControlledTagSet(
        object=ObjectTag.PRESENTATION_SLIDE,
        workflow={WorkflowTag.RESEARCH, WorkflowTag.VERIFICATION},
        safety={SafetyTag.READ_ONLY, SafetyTag.NO_SEND, SafetyTag.NO_POST},
        evidence={EvidenceTag.EXTRACTED, EvidenceTag.PROVIDER_VERIFIED},
        storage={StorageTag.ARTIFACT_FILE, StorageTag.SQLITE},
    )

    assert tags.canonical_tags() == [
        "kba:object:presentation_slide",
        "kba:workflow:research",
        "kba:workflow:verification",
        "kba:safety:no_post",
        "kba:safety:no_send",
        "kba:safety:read_only",
        "kba:evidence:extracted",
        "kba:evidence:provider_verified",
        "kba:storage:artifact_file",
        "kba:storage:sqlite",
    ]


def test_aliases_normalize_but_unknown_tags_fail_closed() -> None:
    assert normalize_controlled_tag("no-send") == "kba:safety:no_send"
    assert normalize_controlled_tag("provider-readback") == (
        "kba:evidence:provider_verified"
    )
    with pytest.raises(ValueError, match="Unsupported"):
        normalize_controlled_tag("important")
    with pytest.raises(ValueError, match="Unsupported evidence"):
        normalize_controlled_tag("kba:evidence:probably_true")


def test_safety_and_storage_incompatibilities_fail_closed() -> None:
    with pytest.raises(ValueError, match="approval_required"):
        ControlledTagSet(
            object=ObjectTag.MESSAGE,
            safety={SafetyTag.EXTERNAL_SIDE_EFFECT},
            storage={StorageTag.PROVIDER},
        )
    with pytest.raises(ValueError, match="hosted_store"):
        ControlledTagSet(
            object=ObjectTag.SOURCE,
            safety={SafetyTag.CONTAINS_SENSITIVE},
            storage={StorageTag.HOSTED_STORE},
        )
    with pytest.raises(ValueError, match="read_only"):
        ControlledTagSet(
            object=ObjectTag.RECORD,
            safety={SafetyTag.READ_ONLY, SafetyTag.INTERNAL_WRITE},
            storage={StorageTag.PROVIDER},
        )


def test_metadata_attachment_is_versioned_and_deduplicated() -> None:
    tags = ControlledTagSet(
        object=ObjectTag.ARTIFACT,
        workflow={WorkflowTag.PLANNING},
        safety={SafetyTag.DRAFT_ONLY},
        evidence={EvidenceTag.SYNTHESIZED},
        storage={StorageTag.WORK_ITEM},
    )

    payload = attach_controlled_tags(
        {"controlled_tags": ["review-only", "kba:safety:draft_only"]}, tags
    )

    assert payload["controlled_vocabulary_version"] == "keystone.workflow_vocabulary.v1"
    assert payload["controlled_tags"] == sorted(set(payload["controlled_tags"]))
    assert "kba:workflow:approval_review" in payload["controlled_tags"]
    assert "kba:workflow:planning" in payload["controlled_tags"]
