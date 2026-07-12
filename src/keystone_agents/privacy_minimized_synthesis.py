"""Typed, fail-before-network minimization for private operational synthesis."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

PACKET_SCHEMA = "keystone.privacy_minimized_synthesis.v1"
PROOF_SCOPE = "sanitized_context_proof"
MAX_PACKET_CHARS = 16_000

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_HTTP_RE = re.compile(r"https?://", re.I)
_ABSOLUTE_PATH_RE = re.compile(r"(?:/Users/|/home/|[A-Z]:\\)", re.I)
_CURRENCY_RE = re.compile(r"(?:[$€£]\s*\d|\b\d[\d,]*\.\d{2}\s*(?:usd|eur|gbp)\b)", re.I)
_SECRET_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{20,}\b|\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)",
    re.I,
)
_RAW_ID_RE = re.compile(
    r"\b(?:thread|message|draft|document|event|record|work[_ -]?item|run)[_-]"
    r"[A-Za-z0-9]{4,}\b",
    re.I,
)
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_HASH_RE = re.compile(r"^[a-f0-9]{12,64}$")


class PrivacyMinimizedFact(BaseModel):
    """One allowlisted semantic signal derived locally from private context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    concept: str
    status: Literal["present", "absent", "unknown"] = "present"
    evidence_count: int = Field(default=1, ge=0, le=999)
    source_hashes: tuple[str, ...] = ()

    @field_validator("concept")
    @classmethod
    def _validate_concept(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SAFE_TOKEN_RE.fullmatch(normalized):
            raise ValueError("Privacy-minimized concepts must be stable allowlisted tokens.")
        return normalized

    @field_validator("source_hashes")
    @classmethod
    def _validate_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item or "").strip().lower() for item in value))
        if any(not _HASH_RE.fullmatch(item) for item in normalized):
            raise ValueError("Privacy-minimized source references must be one-way hashes.")
        return normalized


class PrivacyMinimizedAssertion(BaseModel):
    """One non-identifying subject-predicate-object relationship derived locally."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    predicate: str
    object: str
    count: int = Field(default=1, ge=0, le=999)
    source_hashes: tuple[str, ...] = ()

    @field_validator("subject", "predicate", "object")
    @classmethod
    def _validate_tokens(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SAFE_TOKEN_RE.fullmatch(normalized):
            raise ValueError("Privacy-minimized assertions require stable tokens.")
        return normalized

    @field_validator("source_hashes")
    @classmethod
    def _validate_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item or "").strip().lower() for item in value))
        if any(not _HASH_RE.fullmatch(item) for item in normalized):
            raise ValueError("Assertion source references must be one-way hashes.")
        return normalized


class PrivacyMinimizedSynthesisPacket(BaseModel):
    """The only private-workflow payload allowed to cross the model boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal[PACKET_SCHEMA] = Field(default=PACKET_SCHEMA, alias="schema")
    proof_scope: Literal[PROOF_SCOPE] = PROOF_SCOPE
    workflow: str
    source_count: int = Field(ge=1, le=999)
    source_hashes: tuple[str, ...]
    facts: tuple[PrivacyMinimizedFact, ...]
    assertions: tuple[PrivacyMinimizedAssertion, ...] = ()
    constraints: tuple[str, ...]
    transmission_contract: dict[str, bool] = Field(
        default_factory=lambda: {
            "raw_text": False,
            "personal_identifiers": False,
            "provider_identifiers": False,
            "file_paths": False,
            "urls": False,
            "exact_financial_values": False,
            "secrets": False,
            "phi": False,
        }
    )

    @computed_field
    @property
    def source_ids(self) -> tuple[str, ...]:
        """Return citation identities deterministically derived from source hashes."""

        return tuple(sanitized_source_id(item) for item in self.source_hashes)

    @field_validator("workflow")
    @classmethod
    def _validate_workflow(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SAFE_TOKEN_RE.fullmatch(normalized):
            raise ValueError("Privacy-minimized workflow must be a stable token.")
        return normalized

    @field_validator("source_hashes")
    @classmethod
    def _validate_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item or "").strip().lower() for item in value))
        if not normalized or any(not _HASH_RE.fullmatch(item) for item in normalized):
            raise ValueError("Packet source references must be non-empty one-way hashes.")
        return normalized

    @field_validator("facts")
    @classmethod
    def _validate_facts(
        cls, value: tuple[PrivacyMinimizedFact, ...]
    ) -> tuple[PrivacyMinimizedFact, ...]:
        if not value:
            raise ValueError("Privacy-minimized packet requires at least one semantic fact.")
        concepts = [fact.concept for fact in value]
        if len(concepts) != len(set(concepts)):
            raise ValueError("Privacy-minimized packet concepts must be unique.")
        return value

    @field_validator("constraints")
    @classmethod
    def _validate_constraints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item or "").strip().lower() for item in value))
        if not normalized or any(not _SAFE_TOKEN_RE.fullmatch(item) for item in normalized):
            raise ValueError("Packet constraints must be stable allowlisted tokens.")
        return normalized


def build_concept_signal_packet(
    *,
    workflow: str,
    sources: Mapping[str, str],
    taxonomy: Mapping[str, Sequence[str]],
    constraints: Sequence[str],
) -> PrivacyMinimizedSynthesisPacket:
    """Convert local prose to allowlisted concept signals without copying prose."""

    if not sources:
        raise ValueError("Privacy minimization requires at least one local source.")
    source_hashes = {
        source_ref: _hash_source(source_ref, text) for source_ref, text in sources.items()
    }
    facts: list[PrivacyMinimizedFact] = []
    for concept, terms in taxonomy.items():
        normalized_concept = str(concept or "").strip().lower()
        evidence_hashes: list[str] = []
        evidence_count = 0
        normalized_terms = tuple(
            term.strip().casefold() for term in terms if str(term or "").strip()
        )
        if not normalized_terms:
            raise ValueError(f"Concept {normalized_concept!r} has no allowlisted terms.")
        for source_ref, text in sources.items():
            lowered = str(text or "").casefold()
            matches = sum(lowered.count(term) for term in normalized_terms)
            if matches:
                evidence_hashes.append(source_hashes[source_ref])
                evidence_count += matches
        if evidence_count:
            facts.append(
                PrivacyMinimizedFact(
                    concept=normalized_concept,
                    status="present",
                    evidence_count=min(evidence_count, 999),
                    source_hashes=tuple(evidence_hashes),
                )
            )
    if not facts:
        raise ValueError("No allowlisted semantic concepts were found in the local sources.")
    packet = PrivacyMinimizedSynthesisPacket(
        workflow=workflow,
        source_count=len(sources),
        source_hashes=tuple(source_hashes.values()),
        facts=tuple(facts),
        constraints=tuple(constraints),
    )
    return validate_privacy_minimized_packet(
        packet,
        forbidden_raw_values=tuple(sources) + tuple(sources.values()),
    )


def validate_privacy_minimized_packet(
    packet: PrivacyMinimizedSynthesisPacket | Mapping[str, Any],
    *,
    forbidden_raw_values: Sequence[str] = (),
) -> PrivacyMinimizedSynthesisPacket:
    """Fail locally if an outbound packet contains private or reconstructable data."""

    validated = (
        packet
        if isinstance(packet, PrivacyMinimizedSynthesisPacket)
        else PrivacyMinimizedSynthesisPacket.model_validate(packet)
    )
    payload = validated.model_dump(mode="json", by_alias=True)
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if len(rendered) > MAX_PACKET_CHARS:
        raise ValueError("Privacy-minimized packet exceeds its maximum size.")
    checks = {
        "email address": _EMAIL_RE,
        "HTTP URL": _HTTP_RE,
        "absolute path": _ABSOLUTE_PATH_RE,
        "exact currency value": _CURRENCY_RE,
        "secret": _SECRET_RE,
        "raw provider identity": _RAW_ID_RE,
    }
    for label, pattern in checks.items():
        if pattern.search(rendered):
            raise ValueError(f"Privacy-minimized packet contains {label}.")
    lowered = rendered.casefold()
    if "[redacted" in lowered:
        raise ValueError("Redaction placeholders are not semantic minimization.")
    for raw_value in forbidden_raw_values:
        normalized = " ".join(str(raw_value or "").split()).casefold()
        if len(normalized) >= 8 and normalized in lowered:
            raise ValueError("Privacy-minimized packet retains a forbidden raw value.")
    return validated


def packet_for_model(packet: PrivacyMinimizedSynthesisPacket) -> dict[str, Any]:
    """Return the validated JSON object supplied to an SDK runner."""

    return validate_privacy_minimized_packet(packet).model_dump(mode="json", by_alias=True)


def sanitized_source_id(source_hash: str) -> str:
    normalized = str(source_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise ValueError("Sanitized source ID requires a one-way hash.")
    return f"sanitized-source:{normalized}"


def _hash_source(source_ref: str, text: str) -> str:
    return hashlib.sha256(f"{source_ref}\n{text}".encode()).hexdigest()[:16]
