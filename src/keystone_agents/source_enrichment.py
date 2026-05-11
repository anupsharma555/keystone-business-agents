"""Local source enrichment helpers for business research."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from keystone_agents.schemas.company_profile import SourceRecord
from keystone_agents.source_quality import score_source_quality


class SourceBundle(BaseModel):
    """LLM-ready source bundle that keeps facts tied to source records."""

    company_name: str = Field(min_length=1)
    company_url: str | None = None
    sources: list[SourceRecord] = Field(default_factory=list)
    claim_candidates: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)


CLAIM_SIGNAL_TERMS = (
    "ai",
    "analytics",
    "behavioral health",
    "clinical",
    "clinical trial",
    "compliance",
    "data",
    "evidence",
    "funding",
    "health",
    "hiring",
    "launch",
    "machine learning",
    "mental health",
    "partner",
    "partnership",
    "platform",
    "product",
    "research",
    "software",
    "study",
    "trial",
    "validation",
)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
NEGATED_SIGNAL_PATTERNS = (
    ("clinical trial", "not a clinical trial"),
    ("clinical trial", "does not run clinical trials"),
    ("ai", "not an ai"),
    ("ai", "does not use ai"),
    ("behavioral health", "not behavioral health"),
    ("hipaa", "not hipaa"),
    ("hipaa", "not hipaa compliant"),
)
FUNDING_SIGNAL_TERMS = (
    "funding",
    "raised",
    "raises",
    "financing",
    "series ",
    "seed",
    "capital",
)
OPERATING_STATUS_ACTIVE_PATTERNS = (
    "builds",
    "provides",
    "offers",
    "platform",
    "product",
    "launched",
    "launches",
    "available",
    "markets",
    "commercializes",
    "active company",
)
OPERATING_STATUS_INACTIVE_PATTERNS = (
    "bankrupt",
    "bankruptcy",
    "chapter 11",
    "liquidat",
    "deadpool",
    "wind down",
    "winding down",
    "shut down",
    "ceased operations",
    "discontinued",
    "no longer available",
)
EMPLOYEE_COUNT_RE = re.compile(
    r"\b(?P<low>\d{1,4})(?:\s*[-–]\s*(?P<high>\d{1,4})|\+)?\s+employees?\b",
    re.I,
)
FUNDING_AMOUNT_RE = re.compile(
    r"\$(?P<amount>\d[\d,.]*)(?:\s*(?P<suffix>[kmb])|(?:\s*(?P<word>million|billion|thousand)))?\b",
    re.I,
)


def extract_clean_text(raw: str | None) -> str:
    """Extract normalized readable text from HTML or plain text."""

    text = str(raw or "")
    if not text.strip():
        return ""
    if re.search(r"<[a-zA-Z][^>]*>", text):
        parser = _TextExtractor()
        parser.feed(text)
        text = " ".join(parser.parts)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_claim_candidates(
    text: str,
    *,
    company_name: str,
    max_claims: int = 8,
) -> list[str]:
    """Extract deterministic source-backed claim candidates from page text."""

    cleaned = extract_clean_text(text)
    if not cleaned:
        return []
    company_terms = [part.lower() for part in re.findall(r"[a-zA-Z0-9]+", company_name)]
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    claims: list[str] = []
    for sentence in sentences:
        candidate = sentence.strip(" \t\r\n-")
        if len(candidate) < 18 or len(candidate) > 280:
            continue
        lowered = candidate.lower()
        mentions_company = any(term and term in lowered for term in company_terms)
        has_signal = any(term in lowered for term in CLAIM_SIGNAL_TERMS)
        if not mentions_company and not has_signal:
            continue
        claims.append(candidate)
        if len(claims) >= max_claims:
            break
    return list(dict.fromkeys(claims))


def normalize_source_record(
    payload: dict[str, Any],
    *,
    company_name: str,
    company_url: str | None = None,
    default_source_id: str = "source:1",
    default_source_type: str = "unknown",
) -> SourceRecord | None:
    """Normalize fetched/search/profile payloads into one source record schema."""

    url = str(
        payload.get("url")
        or payload.get("link")
        or payload.get("company_url")
        or payload.get("linkedin_url")
        or payload.get("profile_url")
        or ""
    ).strip()
    if not url:
        return None
    title = str(payload.get("title") or payload.get("name") or url).strip()
    claims = _payload_claims(payload, company_name=company_name)
    if not claims:
        return None
    declared_source_type = str(payload.get("source_type") or default_source_type).strip()
    quality = score_source_quality(
        url=url,
        title=title,
        declared_source_type=declared_source_type,
        supported_text=" ".join(claims),
        company_url=company_url,
        published_at=payload.get("published_at") or payload.get("date"),
        claim_context="company_fact",
    )
    return SourceRecord(
        source_id=str(payload.get("source_id") or payload.get("id") or default_source_id),
        title=title,
        url=url,
        source_type=quality.source_type,
        supported_claims=claims,
        confidence=round(quality.overall_score / 100, 2),
        published_at=payload.get("published_at") or payload.get("date"),
        source_quality=quality,
    )


def dedupe_and_rank_source_records(
    sources: list[SourceRecord],
    *,
    company_url: str | None = None,
    max_sources: int | None = None,
) -> list[SourceRecord]:
    """Merge duplicate source records and rank by source confidence and usefulness."""

    merged: dict[str, SourceRecord] = {}
    order: dict[str, int] = {}
    for index, source in enumerate(sources):
        if not source.supported_claims:
            continue
        key = _source_key(source)
        existing = merged.get(key)
        if existing is None:
            merged[key] = _ensure_scored(source, company_url=company_url)
            order[key] = index
            continue
        claims = list(dict.fromkeys([*existing.supported_claims, *source.supported_claims]))
        confidence = max(existing.confidence, source.confidence)
        scored = _ensure_scored(source, company_url=company_url)
        existing_score = existing.source_quality.overall_score if existing.source_quality else 0
        scored_score = scored.source_quality.overall_score if scored.source_quality else 0
        base = scored if scored_score > existing_score else existing
        merged[key] = base.model_copy(
            update={
                "source_id": existing.source_id,
                "supported_claims": claims,
                "confidence": confidence,
                "source_quality": (
                    scored.source_quality
                    if scored_score > existing_score
                    else existing.source_quality
                ),
            }
        )

    used_ids: set[str] = set()
    ranked = sorted(
        merged.values(),
        key=lambda source: (
            -_rank_score(source),
            order.get(_source_key(source), 0),
            source.source_id,
        ),
    )
    resolved: list[SourceRecord] = []
    for source in ranked:
        source_id = source.source_id
        if source_id in used_ids:
            source_id = f"{source.source_id}:{len(used_ids) + 1}"
            source = source.model_copy(update={"source_id": source_id})
        used_ids.add(source_id)
        resolved.append(source)
        if max_sources is not None and len(resolved) >= max_sources:
            break
    return resolved


def build_source_bundle(
    *,
    company_name: str,
    sources: list[SourceRecord],
    company_url: str | None = None,
    max_sources: int | None = None,
) -> SourceBundle:
    """Build a source bundle for profile synthesis without adding unbacked facts."""

    ranked_sources = dedupe_and_rank_source_records(
        sources,
        company_url=company_url,
        max_sources=max_sources,
    )
    claim_candidates = list(
        dict.fromkeys(
            claim for source in ranked_sources for claim in source.supported_claims if claim.strip()
        )
    )
    return SourceBundle(
        company_name=company_name,
        company_url=company_url,
        sources=ranked_sources,
        claim_candidates=claim_candidates,
        contradictions=detect_source_contradictions(ranked_sources),
        missing_evidence=source_missing_evidence(
            sources=ranked_sources,
            company_url=company_url,
        ),
    )


def detect_source_contradictions(sources: list[SourceRecord]) -> list[str]:
    """Detect simple deterministic contradictions across source claims."""

    claim_rows = [
        (source.source_id, claim, claim.lower())
        for source in sources
        for claim in source.supported_claims
    ]
    contradictions: list[str] = []
    for signal, negated in NEGATED_SIGNAL_PATTERNS:
        positive = [
            (source_id, claim)
            for source_id, claim, lowered in claim_rows
            if signal in lowered and negated not in lowered
        ]
        negative = [
            (source_id, claim) for source_id, claim, lowered in claim_rows if negated in lowered
        ]
        if positive and negative:
            pos_source, pos_claim = positive[0]
            neg_source, neg_claim = negative[0]
            contradictions.append(
                f"Contradictory source claims for {signal}: "
                f"{pos_source} says {pos_claim}; {neg_source} says {neg_claim}."
            )
    contradictions.extend(_funding_contradictions(claim_rows))
    contradictions.extend(_employee_count_contradictions(claim_rows))
    contradictions.extend(_operating_status_contradictions(claim_rows))
    return list(dict.fromkeys(contradictions))


def _funding_amount_millions(text: str) -> float | None:
    if not any(term in text.lower() for term in FUNDING_SIGNAL_TERMS):
        return None
    match = FUNDING_AMOUNT_RE.search(text)
    if match is None:
        return None
    try:
        amount = float(match.group("amount").replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group("suffix") or "").lower()
    word = (match.group("word") or "").lower()
    multiplier = 1.0
    if suffix == "b" or word == "billion":
        multiplier = 1000.0
    elif suffix == "m" or word == "million":
        multiplier = 1.0
    elif suffix == "k" or word == "thousand":
        multiplier = 0.001
    elif amount >= 1000:
        multiplier = 0.001
    return round(amount * multiplier, 3)


def _funding_contradictions(
    claim_rows: list[tuple[str, str, str]],
) -> list[str]:
    funding_claims: list[tuple[str, str, float]] = []
    for source_id, claim, lowered in claim_rows:
        amount = _funding_amount_millions(claim)
        if amount is None or not any(term in lowered for term in FUNDING_SIGNAL_TERMS):
            continue
        funding_claims.append((source_id, claim, amount))
    if len(funding_claims) < 2:
        return []

    values = sorted({round(amount, 1) for _, _, amount in funding_claims})
    if len(values) < 2:
        return []
    min_value = min(values)
    max_value = max(values)
    materially_different = (max_value - min_value) >= 25 and (
        min_value == 0 or (max_value - min_value) / max(min_value, 1) >= 0.15
    )
    if not materially_different:
        return []
    first = min(funding_claims, key=lambda item: item[2])
    last = max(funding_claims, key=lambda item: item[2])
    return [
        "Funding totals conflict across sources: "
        f"{first[0]} says {first[1]}; {last[0]} says {last[1]}."
    ]


def _employee_count_range(text: str) -> tuple[int, int] | None:
    match = EMPLOYEE_COUNT_RE.search(text)
    if match is None:
        return None
    try:
        low = int(match.group("low"))
        high = int(match.group("high") or low)
    except ValueError:
        return None
    if "+" in match.group(0):
        high = max(high, low)
    return (low, high)


def _employee_count_contradictions(
    claim_rows: list[tuple[str, str, str]],
) -> list[str]:
    employee_claims: list[tuple[str, str, tuple[int, int]]] = []
    for source_id, claim, lowered in claim_rows:
        if "employee" not in lowered:
            continue
        employee_range = _employee_count_range(claim)
        if employee_range is None:
            continue
        employee_claims.append((source_id, claim, employee_range))
    if len(employee_claims) < 2:
        return []

    sorted_claims = sorted(employee_claims, key=lambda item: item[2][0])
    low_source, low_claim, low_range = sorted_claims[0]
    high_source, high_claim, high_range = sorted_claims[-1]
    overlaps = low_range[1] >= high_range[0]
    materially_different = high_range[0] >= max(low_range[1] + 20, round(low_range[1] * 1.5))
    if overlaps or not materially_different:
        return []
    return [
        "Team size appears to conflict across sources: "
        f"{low_source} says {low_claim}; {high_source} says {high_claim}."
    ]


def _operating_status_contradictions(
    claim_rows: list[tuple[str, str, str]],
) -> list[str]:
    active = [
        (source_id, claim)
        for source_id, claim, lowered in claim_rows
        if any(pattern in lowered for pattern in OPERATING_STATUS_ACTIVE_PATTERNS)
        and not any(pattern in lowered for pattern in OPERATING_STATUS_INACTIVE_PATTERNS)
    ]
    inactive = [
        (source_id, claim)
        for source_id, claim, lowered in claim_rows
        if any(pattern in lowered for pattern in OPERATING_STATUS_INACTIVE_PATTERNS)
    ]
    if not active or not inactive:
        return []
    active_source, active_claim = active[0]
    inactive_source, inactive_claim = inactive[0]
    return [
        "Operational or product status conflicts across sources: "
        f"{active_source} says {active_claim}; {inactive_source} says {inactive_claim}."
    ]


def source_missing_evidence(
    *,
    sources: list[SourceRecord],
    company_url: str | None = None,
    linkedin_url: str | None = None,
) -> list[str]:
    """Return source-level evidence gaps before profile synthesis."""

    missing: list[str] = []
    source_types = {
        source.source_quality.source_type if source.source_quality else source.source_type
        for source in sources
    }
    if company_url and "company_site" not in source_types:
        missing.append("Missing source-backed company website evidence.")
    if linkedin_url and "linkedin" not in source_types:
        missing.append("Missing source-backed LinkedIn or profile evidence.")
    if not sources:
        missing.append("No source records available.")
    if len({_source_domain(source.url) for source in sources if source.url}) < 2:
        missing.append("Missing independent corroborating source evidence.")
    stale_sources = [
        source.source_id
        for source in sources
        if source.source_quality is not None and source.source_quality.recency_score < 50
    ]
    if stale_sources:
        missing.append(f"Stale source evidence needs review: {', '.join(stale_sources)}.")
    return list(dict.fromkeys(missing))


def source_supports_text(
    *,
    text: str,
    source_ids: list[str],
    source_by_id: dict[str, SourceRecord],
) -> bool:
    """Return whether a free-text value is reasonably supported by its source claims."""

    value_tokens = _meaningful_tokens(text)
    if not value_tokens:
        return False
    for source_id in source_ids:
        source = source_by_id.get(source_id)
        if source is None:
            continue
        source_text = " ".join(source.supported_claims)
        source_tokens = _meaningful_tokens(source_text)
        if not source_tokens:
            continue
        normalized_value = _normalized_text(text)
        normalized_source = _normalized_text(source_text)
        if normalized_value and normalized_value in normalized_source:
            return True
        overlap = len(value_tokens.intersection(source_tokens)) / max(len(value_tokens), 1)
        if overlap >= 0.34 and len(value_tokens.intersection(source_tokens)) >= 2:
            return True
    return False


def _payload_claims(payload: dict[str, Any], *, company_name: str) -> list[str]:
    for key in ("supported_claims", "claims", "facts", "signals", "quote_or_fact", "fact"):
        claims = _as_list(payload.get(key))
        if claims:
            return claims
    for key in ("summary", "description", "content", "text_or_markdown", "text", "html"):
        value = payload.get(key)
        if not value:
            continue
        candidates = extract_claim_candidates(str(value), company_name=company_name)
        if candidates:
            return candidates
        cleaned = extract_clean_text(str(value))
        if cleaned and len(cleaned) <= 280:
            return [cleaned]
    return []


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _ensure_scored(source: SourceRecord, *, company_url: str | None) -> SourceRecord:
    if source.source_quality is not None:
        return source
    quality = score_source_quality(
        url=source.url,
        title=source.title,
        declared_source_type=source.source_type,
        supported_text=" ".join(source.supported_claims),
        company_url=company_url,
        published_at=source.published_at,
        claim_context="company_fact",
    )
    return source.model_copy(
        update={
            "source_type": quality.source_type,
            "confidence": round(quality.overall_score / 100, 2),
            "source_quality": quality,
        }
    )


def _rank_score(source: SourceRecord) -> int:
    quality = source.source_quality.overall_score if source.source_quality else 0
    source_type = source.source_quality.source_type if source.source_quality else source.source_type
    type_bonus = {
        "company_site": 14,
        "news": 20,
        "government": 8,
        "academic": 2,
        "funding_database": 5,
        "fixture": 4,
        "linkedin": 0,
        "google_search": -4,
        "social": -18,
        "unknown": -20,
    }.get(str(source_type), -6)
    signal_bonus = 6 if _has_signal_claim(source) else 0
    claim_bonus = min(6, len(source.supported_claims) * 2)
    recency_penalty = (
        8 if source.source_quality is not None and source.source_quality.recency_score < 50 else 0
    )
    return quality + type_bonus + signal_bonus + claim_bonus - recency_penalty


def _has_signal_claim(source: SourceRecord) -> bool:
    text = " ".join(source.supported_claims).lower()
    return any(
        term in text
        for term in ("funding", "launch", "partnership", "study", "trial", "validation")
    )


def _source_key(source: SourceRecord) -> str:
    url = _canonical_url(source.url)
    if url:
        return url
    claims = "|".join(sorted(_normalized_text(claim) for claim in source.supported_claims))
    return f"{source.source_type}:{claims}"


def _canonical_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("fixture://"):
        return text.lower()
    parsed = urlparse(text if "://" in text else f"https://{text}")
    path = re.sub(r"/+$", "", parsed.path)
    return urlunparse(
        (
            parsed.scheme.lower() or "https",
            parsed.netloc.lower().removeprefix("www."),
            path,
            "",
            "",
            "",
        )
    )


def _source_domain(url: str) -> str:
    if url.startswith("fixture://"):
        return url.split("/", 3)[2] if "://" in url else url
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _normalized_text(value: str) -> str:
    return " ".join(value.lower().split())


def _meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9]+", value.lower())
        if len(token) > 2 and token not in STOPWORDS
    }
