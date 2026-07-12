"""Minimal controlled vocabulary for cross-system KBA workflow context."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ObjectTag(StrEnum):
    WORK_ITEM = "work_item"
    SOURCE = "source"
    ARTIFACT = "artifact"
    MESSAGE = "message"
    RECORD = "record"
    DOCUMENT = "document"
    PRESENTATION_SLIDE = "presentation_slide"
    MEMORY = "memory"


class WorkflowTag(StrEnum):
    DISCOVERY = "discovery"
    RESEARCH = "research"
    TRIAGE = "triage"
    PLANNING = "planning"
    DRAFTING = "drafting"
    APPROVAL_REVIEW = "approval_review"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"
    MONITORING = "monitoring"


class SafetyTag(StrEnum):
    READ_ONLY = "read_only"
    DRAFT_ONLY = "draft_only"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    APPROVAL_REQUIRED = "approval_required"
    CONTAINS_SENSITIVE = "contains_sensitive"
    NO_PHI = "no_phi"
    NO_SEND = "no_send"
    NO_POST = "no_post"


class EvidenceTag(StrEnum):
    USER_PROVIDED = "user_provided"
    PROVIDER_VERIFIED = "provider_verified"
    FIRST_PARTY = "first_party"
    EXTRACTED = "extracted"
    SYNTHESIZED = "synthesized"
    INFERRED = "inferred"
    STALE = "stale"
    MISSING = "missing"
    CONTRADICTED = "contradicted"


class StorageTag(StrEnum):
    TRANSIENT = "transient"
    WORK_ITEM = "work_item"
    SQLITE = "sqlite"
    PROVIDER = "provider"
    ARTIFACT_FILE = "artifact_file"
    HOSTED_STORE = "hosted_store"


class ControlledTagSet(BaseModel):
    """Small interoperable tag set preserved across tools and handoffs."""

    object: ObjectTag
    workflow: set[WorkflowTag] = Field(default_factory=set)
    safety: set[SafetyTag] = Field(default_factory=set)
    evidence: set[EvidenceTag] = Field(default_factory=set)
    storage: set[StorageTag] = Field(default_factory=set)

    @model_validator(mode="after")
    def validate_compatibility(self) -> ControlledTagSet:
        if (
            SafetyTag.EXTERNAL_SIDE_EFFECT in self.safety
            and SafetyTag.APPROVAL_REQUIRED not in self.safety
        ):
            raise ValueError("External side effects require the approval_required tag.")
        if (
            SafetyTag.CONTAINS_SENSITIVE in self.safety
            and StorageTag.HOSTED_STORE in self.storage
        ):
            raise ValueError("Sensitive context cannot be tagged for hosted_store.")
        if SafetyTag.READ_ONLY in self.safety and any(
            tag in self.safety
            for tag in (SafetyTag.INTERNAL_WRITE, SafetyTag.EXTERNAL_SIDE_EFFECT)
        ):
            raise ValueError("read_only cannot be combined with write or side-effect tags.")
        if (
            self.object is ObjectTag.PRESENTATION_SLIDE
            and not self.storage.intersection({StorageTag.ARTIFACT_FILE, StorageTag.SQLITE})
        ):
            raise ValueError(
                "presentation_slide requires artifact_file or sqlite storage provenance."
            )
        return self

    def canonical_tags(self) -> list[str]:
        values = [f"kba:object:{self.object.value}"]
        for dimension, tags in (
            ("workflow", self.workflow),
            ("safety", self.safety),
            ("evidence", self.evidence),
            ("storage", self.storage),
        ):
            values.extend(f"kba:{dimension}:{tag.value}" for tag in sorted(tags))
        return values


_ALIASES = {
    "no-write": "kba:safety:read_only",
    "no_send": "kba:safety:no_send",
    "no-send": "kba:safety:no_send",
    "no_post": "kba:safety:no_post",
    "no-post": "kba:safety:no_post",
    "review-only": "kba:workflow:approval_review",
    "provider-readback": "kba:evidence:provider_verified",
    "local-sqlite": "kba:storage:sqlite",
}


def normalize_controlled_tag(value: str) -> str:
    """Normalize a supported alias or validate one canonical KBA tag."""

    cleaned = str(value or "").strip().casefold().replace(" ", "_")
    cleaned = _ALIASES.get(cleaned, cleaned)
    parts = cleaned.split(":")
    if len(parts) != 3 or parts[0] != "kba":
        raise ValueError(f"Unsupported controlled tag: {value!r}.")
    dimension, member = parts[1], parts[2]
    enums = {
        "object": ObjectTag,
        "workflow": WorkflowTag,
        "safety": SafetyTag,
        "evidence": EvidenceTag,
        "storage": StorageTag,
    }
    enum_type = enums.get(dimension)
    if enum_type is None:
        raise ValueError(f"Unsupported controlled tag dimension: {dimension!r}.")
    try:
        enum_type(member)
    except ValueError as exc:
        raise ValueError(f"Unsupported {dimension} tag value: {member!r}.") from exc
    return f"kba:{dimension}:{member}"


def attach_controlled_tags(metadata: dict[str, Any], tags: ControlledTagSet) -> dict[str, Any]:
    """Return copied metadata with one canonical, deduplicated vocabulary field."""

    payload = dict(metadata)
    existing = [normalize_controlled_tag(item) for item in payload.get("controlled_tags", [])]
    payload["controlled_tags"] = sorted(set([*existing, *tags.canonical_tags()]))
    payload["controlled_vocabulary_version"] = "keystone.workflow_vocabulary.v1"
    return payload
