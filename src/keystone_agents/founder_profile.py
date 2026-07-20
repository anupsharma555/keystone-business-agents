"""Approved founder-fit profile loading and CV review helpers."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pydantic import BaseModel, Field, field_validator

from keystone_agents.schemas.company_profile import ClaimEvidenceRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS_ROOT = PROJECT_ROOT / "documents"
DEFAULT_FOUNDER_FIT_PROFILE_PATH = DOCUMENTS_ROOT / "founder_fit_profile.json"


class FounderFitProfile(BaseModel):
    """Reviewed founder context safe to pass into agent prompts."""

    profile_id: str = Field(default="founder_fit_profile", min_length=1)
    approved_for_search: bool = False
    approved_for_drafting: bool = False
    summary: str = ""
    search_fit_keywords: list[str] = Field(default_factory=list)
    role_targets: list[str] = Field(default_factory=list)
    role_exclusions: list[str] = Field(default_factory=list)
    fit_dimensions: list[str] = Field(default_factory=list)
    strategic_priorities: list[str] = Field(default_factory=list)
    opportunity_lanes: list[str] = Field(default_factory=list)
    access_preferences: list[str] = Field(default_factory=list)
    allowed_outreach_claims: list[str] = Field(default_factory=list)
    reply_context: list[str] = Field(default_factory=list)
    public_links: list[str] = Field(default_factory=list)
    sensitive_fields_removed: list[str] = Field(default_factory=list)
    source_document: str = ""
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        "search_fit_keywords",
        "role_targets",
        "role_exclusions",
        "fit_dimensions",
        "strategic_priorities",
        "opportunity_lanes",
        "access_preferences",
        "allowed_outreach_claims",
        "reply_context",
        "public_links",
        "sensitive_fields_removed",
        "notes",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("founder profile list fields must be lists")
        return [str(item).strip() for item in value if str(item).strip()]


def resolve_founder_fit_profile_path(path_value: str | Path | None) -> Path | None:
    """Resolve a profile path from repo-relative or documents-relative input."""

    if path_value is None or not str(path_value).strip():
        return None
    raw_path = Path(str(path_value)).expanduser()
    candidates = [raw_path]
    if not raw_path.is_absolute():
        candidates.extend([PROJECT_ROOT / raw_path, DOCUMENTS_ROOT / raw_path])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_founder_fit_profile(path_value: str | Path | None) -> FounderFitProfile | None:
    """Load an optional approved founder-fit profile from JSON."""

    path = resolve_founder_fit_profile_path(path_value)
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"Founder fit profile not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Founder fit profile must be a JSON object.")
    return FounderFitProfile.model_validate(data)


def founder_search_context(profile: FounderFitProfile | None) -> str:
    """Return search/fit context only when explicitly approved for search."""

    if profile is None:
        return ""
    if not profile.approved_for_search:
        raise ValueError("Founder fit profile must set approved_for_search=true before search use.")
    payload = {
        "profile_id": profile.profile_id,
        "summary": profile.summary,
        "search_fit_keywords": profile.search_fit_keywords,
        "role_targets": profile.role_targets,
        "role_exclusions": profile.role_exclusions,
        "fit_dimensions": profile.fit_dimensions,
        "strategic_priorities": profile.strategic_priorities,
        "opportunity_lanes": profile.opportunity_lanes,
        "access_preferences": profile.access_preferences,
        "public_links": profile.public_links,
        "policy": (
            "Use for search query planning, role-fit assessment, and Keystone-fit "
            "reasoning only. Do not use private CV details as outreach claims."
        ),
    }
    return (
        "Approved founder fit profile for search and fit assessment:\n"
        f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
    )


def founder_drafting_context(profile: FounderFitProfile | None) -> str:
    """Return drafting context only when explicitly approved for drafting."""

    if profile is None:
        return ""
    if not profile.approved_for_drafting:
        return ""
    payload = {
        "profile_id": profile.profile_id,
        "summary": profile.summary,
        "allowed_outreach_claims": profile.allowed_outreach_claims,
        "reply_context": profile.reply_context,
        "public_links": profile.public_links,
        "policy": (
            "Use only allowed_outreach_claims and reply_context. Do not disclose raw CV "
            "details, private identifiers, or unsupported claims."
        ),
    }
    return (
        "Approved founder context for reply or outreach drafting:\n"
        f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
    )


def founder_profile_claims(profile: FounderFitProfile | None) -> list[ClaimEvidenceRecord]:
    """Convert approved founder drafting claims into source-backed claim records."""

    if profile is None or not profile.approved_for_drafting:
        return []
    return [
        ClaimEvidenceRecord(
            claim_text=claim,
            source_id=f"founder_fit_profile:{profile.profile_id}",
            confidence=1.0,
            claim_type="keystone_profile",
        )
        for claim in profile.allowed_outreach_claims
    ]


def founder_profile_audit_payload(
    path_value: str | Path | None,
    profile: FounderFitProfile | None,
) -> dict[str, Any]:
    """Return non-sensitive metadata for agent-run audit payloads."""

    search_context_fields = []
    if profile:
        search_context_fields = [
            field_name
            for field_name in (
                "summary",
                "search_fit_keywords",
                "fit_dimensions",
                "strategic_priorities",
                "opportunity_lanes",
                "access_preferences",
                "public_links",
            )
            if getattr(profile, field_name)
        ]
    required_search_fields = {
        "summary",
        "search_fit_keywords",
        "fit_dimensions",
        "strategic_priorities",
        "opportunity_lanes",
        "access_preferences",
        "public_links",
    }
    return {
        "path": str(path_value or ""),
        "profile_id": profile.profile_id if profile else "",
        "approved_for_search": bool(profile and profile.approved_for_search),
        "approved_for_drafting": bool(profile and profile.approved_for_drafting),
        "search_context_fields": search_context_fields,
        "search_context_complete": bool(
            profile
            and profile.approved_for_search
            and required_search_fields <= set(search_context_fields)
        ),
    }


def extract_docx_text(path_value: str | Path) -> str:
    """Extract plain text from a .docx using only the Python standard library."""

    path = Path(path_value).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"CV document not found: {path}")
    paragraphs: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name == "word/document.xml"
            or (name.startswith("word/header") and name.endswith(".xml"))
            or (name.startswith("word/footer") and name.endswith(".xml"))
        ]
        for name in names:
            root = ElementTree.fromstring(archive.read(name))
            namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            for paragraph in root.findall(".//w:p", namespace):
                text = "".join(
                    node.text or "" for node in paragraph.findall(".//w:t", namespace)
                ).strip()
                if text:
                    paragraphs.append(text)
    return "\n".join(paragraphs)


def build_founder_cv_review_packet(
    cv_path: str | Path,
    *,
    include_extracted_text: bool = False,
) -> dict[str, Any]:
    """Build a local review packet for turning a CV into a founder-fit profile."""

    text = extract_docx_text(cv_path)
    packet: dict[str, Any] = {
        "status": "review_required",
        "source_document": str(cv_path),
        "character_count": len(text),
        "paragraph_count": len([line for line in text.splitlines() if line.strip()]),
        "redaction_checklist": [
            "Remove PHI and patient-specific details.",
            "Remove personal IDs, home address, private phone numbers, and credentials.",
            "Remove secrets, private contract terms, and private compensation details.",
            "Convert only approved professional-fit facts into founder_fit_profile.json.",
        ],
        "profile_template": FounderFitProfile(
            profile_id="founder_fit_2026",
            approved_for_search=False,
            approved_for_drafting=False,
        ).model_dump(mode="json"),
    }
    if include_extracted_text:
        packet["extracted_text"] = text
    return packet
