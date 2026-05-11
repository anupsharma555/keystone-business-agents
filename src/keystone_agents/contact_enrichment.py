"""Source-backed contact candidate extraction for outreach readiness."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class ContactCandidate(BaseModel):
    """Potential outreach contact or channel with source attribution."""

    company_name: str = ""
    name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    source_url: str = ""
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verification_status: str = "needs_confirmation"


class ContactEnrichmentArtifact(BaseModel):
    """First-class contact-enrichment artifact for WorkItems."""

    company_name: str
    candidates: list[ContactCandidate] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)
    send_enabled: bool = False


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
LINKEDIN_PERSON_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+", re.I)
CONTACT_TITLE_MARKERS = (
    "business development",
    "partnership",
    "partnerships",
    "clinical",
    "medical",
    "research",
    "commercial",
    "growth",
)


def build_contact_enrichment_artifact(
    *,
    company_name: str,
    sources: list[Any],
) -> ContactEnrichmentArtifact:
    """Extract conservative contact candidates from source records."""

    candidates: list[ContactCandidate] = []
    source_ids_used: list[str] = []
    for source in sources:
        source_id = str(getattr(source, "source_id", "") or "")
        source_url = str(getattr(source, "url", "") or "")
        text = " ".join(
            [
                str(getattr(source, "title", "") or ""),
                str(getattr(source, "url", "") or ""),
                " ".join(str(claim) for claim in getattr(source, "supported_claims", []) or []),
            ]
        )
        emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
        linkedin_urls = list(dict.fromkeys(LINKEDIN_PERSON_RE.findall(text)))
        title = _contact_title_from_text(text)
        for email in emails[:3]:
            candidates.append(
                ContactCandidate(
                    company_name=company_name,
                    email=email,
                    title=title,
                    source_url=source_url,
                    source_ids=[source_id] if source_id else [],
                    confidence=0.78 if _generic_inbox(email) else 0.86,
                    verification_status="source_backed",
                )
            )
            if source_id:
                source_ids_used.append(source_id)
        for linkedin_url in linkedin_urls[:3]:
            candidates.append(
                ContactCandidate(
                    company_name=company_name,
                    linkedin_url=linkedin_url,
                    title=title,
                    source_url=source_url,
                    source_ids=[source_id] if source_id else [],
                    confidence=0.72,
                    verification_status="source_backed",
                )
            )
            if source_id:
                source_ids_used.append(source_id)
    deduped = _dedupe_candidates(candidates)
    missing: list[str] = []
    if not any(candidate.email for candidate in deduped):
        missing.append("No source-backed contact email found.")
    if not any(candidate.linkedin_url for candidate in deduped):
        missing.append("No source-backed person LinkedIn URL found.")
    if not deduped:
        missing.append("Contact enrichment needs stronger source text or contact-specific search.")
    return ContactEnrichmentArtifact(
        company_name=company_name,
        candidates=deduped[:8],
        missing=list(dict.fromkeys(missing)),
        source_ids_used=list(dict.fromkeys(source_ids_used)),
    )


def _contact_title_from_text(text: str) -> str:
    lowered = text.lower()
    for marker in CONTACT_TITLE_MARKERS:
        if marker in lowered:
            return marker.title()
    return ""


def _generic_inbox(email: str) -> bool:
    local = email.split("@", maxsplit=1)[0].lower()
    return local in {"info", "hello", "contact", "support", "partners", "partnerships"}


def _dedupe_candidates(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    deduped: list[ContactCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.email.lower(), candidate.linkedin_url.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped
