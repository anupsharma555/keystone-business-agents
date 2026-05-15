"""Source-backed contact candidate extraction for outreach readiness."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from keystone_agents.schemas.recommendation import OpportunityContactPath


class ContactCandidate(BaseModel):
    """Potential outreach contact or channel with source attribution."""

    company_name: str = ""
    name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    path_type: str = "other"
    path_label: str = ""
    path_value: str = ""
    source_url: str = ""
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verification_status: str = "needs_confirmation"


class ContactEnrichmentArtifact(BaseModel):
    """First-class contact-enrichment artifact for WorkItems."""

    company_name: str
    candidates: list[ContactCandidate] = Field(default_factory=list)
    best_contact_path: OpportunityContactPath | None = None
    alternate_contact_paths: list[OpportunityContactPath] = Field(default_factory=list)
    contact_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)
    send_enabled: bool = False


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
LINKEDIN_PERSON_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+", re.I)
LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9_%./-]+",
    re.I,
)
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
ORG_CHANNEL_LOCALPARTS = {
    "bd",
    "businessdevelopment",
    "collaboration",
    "collaborations",
    "contact",
    "hello",
    "info",
    "partnership",
    "partnerships",
    "research",
}
CONTACT_PAGE_MARKERS = (
    "contact",
    "partner",
    "partnership",
    "collaborate",
    "speaker",
    "abstract",
    "submit",
    "submissions",
    "rfp",
    "solicitation",
    "procurement",
)


def build_contact_enrichment_artifact(
    *,
    company_name: str,
    sources: list[Any],
) -> ContactEnrichmentArtifact:
    """Extract conservative contact candidates from source records."""

    candidates: list[ContactCandidate] = []
    paths: list[OpportunityContactPath] = []
    source_ids_used: list[str] = []
    for source in sources:
        source_id = str(getattr(source, "source_id", "") or "")
        source_url = str(getattr(source, "url", "") or "")
        text = " ".join(
            [
                str(getattr(source, "title", "") or ""),
                str(getattr(source, "url", "") or ""),
                str(getattr(source, "supported_signal", "") or ""),
                " ".join(str(claim) for claim in getattr(source, "supported_claims", []) or []),
            ]
        )
        emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
        linkedin_urls = list(dict.fromkeys(LINKEDIN_PERSON_RE.findall(text)))
        linkedin_company_urls = list(dict.fromkeys(LINKEDIN_COMPANY_RE.findall(text)))
        title = _contact_title_from_text(text)
        for email in emails[:3]:
            path = _path_from_email(email, source_id=source_id, source_url=source_url, title=title)
            paths.append(path)
            candidates.append(
                ContactCandidate(
                    company_name=company_name,
                    email=email,
                    title=title,
                    path_type=path.path_type,
                    path_label=path.label,
                    path_value=path.value,
                    source_url=source_url,
                    source_ids=[source_id] if source_id else [],
                    confidence=path.confidence,
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
                    path_type="linkedin",
                    path_label=title or "LinkedIn profile",
                    path_value=linkedin_url,
                    source_url=source_url,
                    source_ids=[source_id] if source_id else [],
                    confidence=0.72,
                    verification_status="source_backed",
                )
            )
            paths.append(
                OpportunityContactPath(
                    path_type="linkedin",
                    label=title or "LinkedIn profile",
                    url=linkedin_url,
                    confidence=0.72,
                    source_id=source_id,
                )
            )
            if source_id:
                source_ids_used.append(source_id)
        for linkedin_url in linkedin_company_urls[:2]:
            paths.append(
                OpportunityContactPath(
                    path_type="linkedin",
                    label="Company LinkedIn page",
                    url=linkedin_url,
                    confidence=0.58,
                    source_id=source_id,
                    needs_confirmation=True,
                )
            )
            if source_id:
                source_ids_used.append(source_id)
        org_path = _org_contact_path_from_source(
            text=text,
            source_id=source_id,
            source_url=source_url,
        )
        if org_path is not None:
            paths.append(org_path)
            if source_id:
                source_ids_used.append(source_id)
    deduped = _dedupe_candidates(candidates)
    ranked_paths = _rank_contact_paths(paths)
    best_path = ranked_paths[0] if ranked_paths else None
    missing: list[str] = []
    if not any(candidate.email for candidate in deduped):
        missing.append("No source-backed contact email found.")
    if not any(candidate.linkedin_url for candidate in deduped):
        missing.append("No source-backed person LinkedIn URL found.")
    if best_path is None:
        missing.append("No source-backed organization contact path found.")
        missing.append("Contact enrichment needs stronger source text or contact-specific search.")
    return ContactEnrichmentArtifact(
        company_name=company_name,
        candidates=deduped[:8],
        best_contact_path=best_path,
        alternate_contact_paths=ranked_paths[1:8],
        contact_confidence=best_path.confidence if best_path is not None else 0.0,
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
    return local in ORG_CHANNEL_LOCALPARTS or local in {"support"}


def _path_from_email(
    email: str,
    *,
    source_id: str,
    source_url: str,
    title: str,
) -> OpportunityContactPath:
    generic = _generic_inbox(email)
    return OpportunityContactPath(
        path_type="email",
        label=title or ("Organization inbox" if generic else "Source-backed email"),
        value=email,
        confidence=0.74 if generic else 0.9,
        source_id=source_id,
        needs_confirmation=generic,
        url=source_url,
    )


def _org_contact_path_from_source(
    *,
    text: str,
    source_id: str,
    source_url: str,
) -> OpportunityContactPath | None:
    if not source_url or source_url.startswith("fixture://"):
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in CONTACT_PAGE_MARKERS):
        return None
    path_type = "form"
    label = "Organization contact page"
    if any(marker in lowered for marker in ("conference", "abstract", "speaker", "submit")):
        path_type = "conference_portal"
        label = "Submission or conference portal"
    elif any(marker in lowered for marker in ("rfp", "solicitation", "procurement")):
        path_type = "website"
        label = "Procurement or RFP page"
    return OpportunityContactPath(
        path_type=path_type,  # type: ignore[arg-type]
        label=label,
        url=source_url,
        confidence=0.62,
        source_id=source_id,
        needs_confirmation=True,
    )


def _rank_contact_paths(paths: list[OpportunityContactPath]) -> list[OpportunityContactPath]:
    deduped: dict[tuple[str, str], OpportunityContactPath] = {}
    for path in paths:
        key = (path.path_type, (path.value or path.url).lower())
        if not key[1]:
            continue
        existing = deduped.get(key)
        if existing is None or path.confidence > existing.confidence:
            deduped[key] = path

    def sort_key(path: OpportunityContactPath) -> tuple[int, float]:
        channel_rank = {
            "email": 5,
            "linkedin": 4,
            "conference_portal": 3,
            "form": 2,
            "website": 1,
            "other": 0,
        }.get(path.path_type, 0)
        return (channel_rank, path.confidence)

    return sorted(deduped.values(), key=sort_key, reverse=True)


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
