"""Deterministic source quality scoring for research and scouting."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

SourceQualityType = Literal[
    "fixture",
    "company_site",
    "linkedin",
    "google_search",
    "news",
    "conference",
    "publication",
    "job_posting",
    "funding_database",
    "academic",
    "government",
    "social",
    "unknown",
]


class SourceQualityScore(BaseModel):
    """Trust, freshness, and relevance score for one source."""

    url: str
    title: str | None = None
    source_type: SourceQualityType
    credibility_score: int = Field(ge=0, le=100)
    domain_credibility_score: int = Field(default=0, ge=0, le=100)
    recency_score: int = Field(ge=0, le=100)
    relevance_score: int = Field(ge=0, le=100)
    overall_score: int = Field(ge=0, le=100)
    rationale: str


class SourceQualitySummary(BaseModel):
    """Aggregate quality signal for a set of sources."""

    source_count: int = Field(ge=0)
    independent_source_count: int = Field(default=0, ge=0)
    average_credibility_score: int = Field(ge=0, le=100)
    average_domain_credibility_score: int = Field(default=0, ge=0, le=100)
    average_recency_score: int = Field(ge=0, le=100)
    average_relevance_score: int = Field(ge=0, le=100)
    overall_score: int = Field(ge=0, le=100)
    high_quality_source_count: int = Field(ge=0)
    low_quality_source_count: int = Field(ge=0)
    rationale: str


class ResearchCompletenessScore(BaseModel):
    """Deterministic signal for whether fixture/mock research is sufficient."""

    source_count: int = Field(ge=0)
    independent_source_count: int = Field(ge=0)
    high_quality_source_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    score: int = Field(ge=0, le=100)
    satisfied_dimensions: list[str] = Field(default_factory=list)
    missing_dimensions: list[str] = Field(default_factory=list)
    stop_recommended: bool = False
    rationale: str


NEWS_DOMAINS = {
    "apnews.com",
    "biopharmadive.com",
    "businesswire.com",
    "endpts.com",
    "fiercebiotech.com",
    "fiercehealthcare.com",
    "healthcareitnews.com",
    "prnewswire.com",
    "reuters.com",
    "statnews.com",
    "techcrunch.com",
}
FUNDING_DOMAINS = {"crunchbase.com", "pitchbook.com", "sec.gov"}
ACADEMIC_DOMAINS = {
    "clinicaltrials.gov",
    "doi.org",
    "jamanetwork.com",
    "nature.com",
    "nejm.org",
    "pubmed.ncbi.nlm.nih.gov",
    "science.org",
    "thelancet.com",
}
SOCIAL_DOMAINS = {
    "bsky.app",
    "facebook.com",
    "instagram.com",
    "medium.com",
    "reddit.com",
    "threads.net",
    "tiktok.com",
    "twitter.com",
    "x.com",
}
SIGNAL_TERMS = (
    "funding",
    "hiring",
    "launch",
    "partnership",
    "pilot",
    "publication",
    "raises",
    "study",
    "trial",
    "validation",
    "rfp",
    "solicitation",
    "procurement",
    "special issue",
    "call for papers",
    "call for manuscripts",
)
COMPANY_FACT_TERMS = ("about", "company", "leadership", "platform", "product", "team")


def score_source_quality(
    *,
    url: str,
    title: str | None = None,
    declared_source_type: str | None = None,
    supported_text: str | None = None,
    company_url: str | None = None,
    published_at: str | date | datetime | None = None,
    as_of_date: date | None = None,
    claim_context: str = "company_fact",
) -> SourceQualityScore:
    """Score one source without network calls."""

    normalized_type = infer_source_type(
        url=url,
        declared_source_type=declared_source_type,
        company_url=company_url,
    )
    credibility = _credibility_score(normalized_type, claim_context)
    domain_credibility = _domain_credibility_score(
        url=url,
        source_type=normalized_type,
        company_url=company_url,
    )
    recency = _recency_score(published_at, as_of_date=as_of_date, source_type=normalized_type)
    relevance = _relevance_score(
        normalized_type,
        text=" ".join(item for item in (title, url, supported_text) if item),
        claim_context=claim_context,
    )
    overall = round(
        credibility * 0.35 + domain_credibility * 0.20 + recency * 0.20 + relevance * 0.25
    )
    return SourceQualityScore(
        url=url,
        title=title,
        source_type=normalized_type,
        credibility_score=credibility,
        domain_credibility_score=domain_credibility,
        recency_score=recency,
        relevance_score=relevance,
        overall_score=max(0, min(100, overall)),
        rationale=_rationale(
            normalized_type,
            credibility,
            domain_credibility,
            recency,
            relevance,
            claim_context,
        ),
    )


def summarize_source_quality(scores: list[SourceQualityScore]) -> SourceQualitySummary:
    """Combine per-source quality scores into one summary."""

    if not scores:
        return SourceQualitySummary(
            source_count=0,
            independent_source_count=0,
            average_credibility_score=0,
            average_domain_credibility_score=0,
            average_recency_score=0,
            average_relevance_score=0,
            overall_score=0,
            high_quality_source_count=0,
            low_quality_source_count=0,
            rationale="No sources available; source confidence is low.",
        )

    overall = combined_source_confidence(scores)
    independent_sources = independent_source_count(scores)
    high_quality = sum(1 for score in scores if score.overall_score >= 75)
    low_quality = sum(1 for score in scores if score.overall_score < 50)
    return SourceQualitySummary(
        source_count=len(scores),
        independent_source_count=independent_sources,
        average_credibility_score=_average(score.credibility_score for score in scores),
        average_domain_credibility_score=_average(
            score.domain_credibility_score for score in scores
        ),
        average_recency_score=_average(score.recency_score for score in scores),
        average_relevance_score=_average(score.relevance_score for score in scores),
        overall_score=overall,
        high_quality_source_count=high_quality,
        low_quality_source_count=low_quality,
        rationale=(
            f"{high_quality} high-quality source(s), {low_quality} low-quality source(s), "
            f"{independent_sources} independent source(s), combined confidence {overall}/100."
        ),
    )


def assess_research_completeness(
    *,
    source_scores: Sequence[SourceQualityScore],
    evidence: Sequence[str],
    unsupported_claims: Sequence[str] = (),
) -> ResearchCompletenessScore:
    """Assess whether fixture/mock company research has enough trusted evidence."""

    scores = list(source_scores)
    evidence_items = [str(item).strip() for item in evidence if str(item).strip()]
    summary = summarize_source_quality(scores)
    evidence_text = " ".join(evidence_items).lower()
    has_company_site = any(
        score.source_type == "company_site" and score.overall_score >= 72 for score in scores
    )
    has_independent_corroboration = (
        summary.independent_source_count >= 2 and summary.high_quality_source_count >= 2
    )
    has_recent_signal = any(term in evidence_text for term in SIGNAL_TERMS)
    has_core_company_fact = bool(evidence_items)
    has_domain_credible_source = any(score.domain_credibility_score >= 80 for score in scores)

    satisfied: list[str] = []
    missing: list[str] = []
    if has_company_site:
        satisfied.append("company website evidence")
    else:
        missing.append("trusted company website evidence")
    if has_independent_corroboration:
        satisfied.append("independent corroboration")
    else:
        missing.append("at least two high-quality independent sources")
    if has_recent_signal:
        satisfied.append("recent or strategic signal")
    else:
        missing.append("recent funding, partnership, validation, launch, or hiring signal")
    if has_core_company_fact:
        satisfied.append("source-backed company facts")
    else:
        missing.append("source-backed company facts")
    if has_domain_credible_source:
        satisfied.append("credible source domain")
    else:
        missing.append("credible source domain")

    evidence_component = min(15, len(evidence_items) * 3)
    corroboration_component = min(20, max(0, summary.independent_source_count - 1) * 10)
    high_quality_component = min(20, summary.high_quality_source_count * 10)
    score = round(
        (20 if has_company_site else 0)
        + corroboration_component
        + high_quality_component
        + evidence_component
        + (10 if has_recent_signal else 0)
        + (summary.overall_score * 0.15)
    )
    if unsupported_claims:
        score -= min(12, len(unsupported_claims) * 4)
    bounded_score = max(0, min(100, score))
    stop_recommended = (
        bounded_score >= 82
        and has_company_site
        and has_independent_corroboration
        and len(evidence_items) >= 3
    )
    return ResearchCompletenessScore(
        source_count=summary.source_count,
        independent_source_count=summary.independent_source_count,
        high_quality_source_count=summary.high_quality_source_count,
        evidence_count=len(evidence_items),
        score=bounded_score,
        satisfied_dimensions=satisfied,
        missing_dimensions=missing,
        stop_recommended=stop_recommended,
        rationale=(
            f"Research completeness {bounded_score}/100; "
            f"satisfied: {', '.join(satisfied) if satisfied else 'none'}; "
            f"missing: {', '.join(missing) if missing else 'none'}."
        ),
    )


def combined_source_confidence(scores: list[SourceQualityScore]) -> int:
    """Return a bounded combined source confidence score."""

    if not scores:
        return 0
    ranked = sorted((score.overall_score for score in scores), reverse=True)
    best = ranked[0]
    corroboration_bonus = min(16, max(0, independent_source_count(scores) - 1) * 5)
    low_quality_penalty = sum(1 for value in ranked if value < 50) * 3
    combined = round(best * 0.7 + _average(ranked) * 0.3)
    return max(0, min(100, combined + corroboration_bonus - low_quality_penalty))


def independent_source_count(scores: list[SourceQualityScore]) -> int:
    """Count unique source domains or fixture namespaces."""

    keys: set[str] = set()
    for score in scores:
        key = _source_identity_key(score.url)
        if key:
            keys.add(key)
    return len(keys)


def infer_source_type(
    *,
    url: str,
    declared_source_type: str | None = None,
    company_url: str | None = None,
) -> SourceQualityType:
    """Normalize source labels and domains into Keystone quality categories."""

    declared = (declared_source_type or "").strip().lower()
    if declared in {"company_site", "company_website", "website"}:
        return "company_site"
    if declared == "fixture":
        return "fixture"
    if declared in {"google_search", "search", "metasearch"}:
        return "google_search"
    if declared == "linkedin":
        return "linkedin"
    if declared in {"funding_database", "database"}:
        return "funding_database"
    if declared == "publication":
        return "publication"
    if declared == "conference":
        return "conference"
    if declared in {"job_posting", "careers", "job"}:
        return "job_posting"
    if declared in {"academic", "clinical_trial"}:
        return "academic"
    if declared == "government":
        return "government"
    if declared in {"social", "social_post"}:
        return "social"
    if declared == "news":
        return "news"

    host = _hostname(url)
    company_host = _hostname(company_url or "")
    if host and company_host and _registered_domain(host) == _registered_domain(company_host):
        return "company_site"
    if "linkedin.com" in host:
        return "linkedin"
    if host.endswith(".gov") or host in {"nih.gov", "fda.gov", "cms.gov"}:
        return "government"
    if host.endswith(".edu") or _matches_domain(host, ACADEMIC_DOMAINS):
        return "academic"
    if _matches_domain(host, FUNDING_DOMAINS):
        return "funding_database"
    if _matches_domain(host, SOCIAL_DOMAINS):
        return "social"
    if _matches_domain(host, NEWS_DOMAINS):
        return "news"
    return "unknown"


def _credibility_score(source_type: SourceQualityType, claim_context: str) -> int:
    context = claim_context.lower()
    if source_type == "fixture":
        return 78
    if source_type == "company_site":
        return 90 if "company" in context or "fact" in context else 74
    if source_type in {"academic", "government", "publication"}:
        return 92
    if source_type == "funding_database":
        return 82
    if source_type == "job_posting":
        return 70
    if source_type == "conference":
        return 68
    if source_type == "news":
        return 74
    if source_type == "linkedin":
        return 64 if "persona" in context or "signal" in context else 56
    if source_type == "google_search":
        return 58 if "signal" in context else 48
    if source_type == "social":
        return 42 if "signal" in context else 30
    return 38


def _domain_credibility_score(
    *,
    url: str,
    source_type: SourceQualityType,
    company_url: str | None,
) -> int:
    host = _hostname(url)
    company_host = _hostname(company_url or "")
    if source_type == "fixture":
        return 72
    if host and company_host and _registered_domain(host) == _registered_domain(company_host):
        return 94
    if host.endswith(".gov") or host in {"nih.gov", "fda.gov", "cms.gov"}:
        return 96
    if host.endswith(".edu") or _matches_domain(host, ACADEMIC_DOMAINS):
        return 92
    if _matches_domain(host, NEWS_DOMAINS):
        return 86
    if _matches_domain(host, FUNDING_DOMAINS):
        return 84
    if "linkedin.com" in host:
        return 64
    if _matches_domain(host, SOCIAL_DOMAINS):
        return 34
    if source_type == "company_site":
        return 82
    if source_type == "publication":
        return 90
    if source_type == "job_posting":
        return 72
    if source_type == "conference":
        return 66
    if source_type == "google_search":
        return 48
    if source_type == "unknown":
        return 36
    return 50


def _recency_score(
    published_at: str | date | datetime | None,
    *,
    as_of_date: date | None,
    source_type: SourceQualityType,
) -> int:
    if published_at is None or published_at == "":
        if source_type in {"company_site", "government", "academic", "fixture"}:
            return 70
        if source_type in {"social", "linkedin", "google_search"}:
            return 58
        return 62

    published_date = _parse_date(published_at)
    if published_date is None:
        return 50
    age_days = ((as_of_date or date.today()) - published_date).days
    if age_days <= 365:
        return 100
    if age_days <= 730:
        return 80
    if age_days <= 1095:
        return 65
    if age_days <= 1825:
        return 45
    return 25


def _relevance_score(
    source_type: SourceQualityType,
    *,
    text: str,
    claim_context: str,
) -> int:
    lowered = text.lower()
    context = claim_context.lower()
    if "opportunity" in context or "signal" in context:
        hits = sum(1 for term in SIGNAL_TERMS if term in lowered)
        base = (
            68
            if source_type
            in {
                "news",
                "conference",
                "publication",
                "job_posting",
                "funding_database",
                "linkedin",
                "social",
                "fixture",
            }
            else 60
        )
        if source_type == "google_search":
            base = 62
        return min(100, base + hits * 8)
    if "company" in context or "fact" in context:
        hits = sum(1 for term in COMPANY_FACT_TERMS if term in lowered)
        base = 86 if source_type == "company_site" else 70
        if source_type in {"social", "unknown"}:
            base = 45
        return min(100, base + hits * 5)
    return 65


def _rationale(
    source_type: SourceQualityType,
    credibility: int,
    domain_credibility: int,
    recency: int,
    relevance: int,
    claim_context: str,
) -> str:
    notes = {
        "fixture": "fixture source is deterministic for dry-run development",
        "company_site": "company website is strong for company-controlled facts",
        "academic": "peer-reviewed or academic source has high credibility",
        "government": "government source has high credibility",
        "google_search": "Google Search result is a discovery pointer and needs verification",
        "news": "reputable news is useful but may require corroboration",
        "conference": "conference signal is useful but should be corroborated",
        "publication": "publication source has high credibility for evidence claims",
        "job_posting": "job posting is useful for hiring and team-growth signals",
        "funding_database": "funding database is useful for financing and company signals",
        "linkedin": "LinkedIn is useful for signal and persona context",
        "social": "social posts are signals, not definitive factual evidence",
        "unknown": "unknown domain lowers confidence",
    }
    return (
        f"{notes[source_type]}; context={claim_context}; source_type_credibility={credibility}, "
        f"domain_credibility={domain_credibility}, recency={recency}, relevance={relevance}."
    )


def _parse_date(value: str | date | datetime) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.date()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _hostname(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.lower().removeprefix("www.")


def _registered_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _source_identity_key(url: str) -> str:
    if url.startswith("fixture://"):
        return url.split("/", 3)[2] if "://" in url else url
    host = _hostname(url)
    if host:
        return _registered_domain(host)
    return url.strip().lower()


def _matches_domain(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _average(values: Any) -> int:
    items = list(values)
    if not items:
        return 0
    return round(sum(items) / len(items))
