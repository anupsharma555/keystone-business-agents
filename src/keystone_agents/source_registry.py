"""Source-lane registry and coverage scoring for web retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

SOURCE_LANES: tuple[str, ...] = (
    "company_site",
    "careers_jobs",
    "clinical_trials",
    "grants_funding",
    "literature",
    "procurement_rfp",
    "conference_events",
    "press_news",
    "people_institutions",
    "regulatory",
)

PRIMARY_SOURCE_LANES = frozenset(
    {
        "company_site",
        "careers_jobs",
        "clinical_trials",
        "grants_funding",
        "literature",
        "procurement_rfp",
        "conference_events",
        "people_institutions",
        "regulatory",
    }
)

COMPANY_SITE_PATH_HINTS = (
    "/about",
    "/about-us",
    "/team",
    "/leadership",
    "/press",
    "/news",
    "/careers",
    "/jobs",
    "/partners",
    "/research",
)
CAREERS_DOMAINS = frozenset(
    {
        "boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
        "apply.workable.com",
    }
)
GRANT_DOMAINS = frozenset(
    {
        "reporter.nih.gov",
        "grants.nih.gov",
        "grants.gov",
        "sbir.gov",
    }
)
LITERATURE_DOMAINS = frozenset(
    {
        "pubmed.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "doi.org",
        "api.crossref.org",
    }
)
PROCUREMENT_DOMAINS = frozenset({"sam.gov"})
REGULATORY_DOMAINS = frozenset({"fda.gov", "cms.gov", "hhs.gov"})
PRESS_DOMAINS = frozenset(
    {
        "businesswire.com",
        "prnewswire.com",
        "fiercehealthcare.com",
        "hitconsultant.net",
        "mobihealthnews.com",
    }
)
AGGREGATOR_DOMAINS = frozenset(
    {
        "crunchbase.com",
        "glassdoor.com",
        "indeed.com",
        "linkedin.com",
        "wellfound.com",
        "wikipedia.org",
    }
)


@dataclass(frozen=True)
class SourceCoverageAssessment:
    """Coverage assessment for a retrieved search packet."""

    expected_lanes: tuple[str, ...] = ()
    observed_lanes: tuple[str, ...] = ()
    missing_lanes: tuple[str, ...] = ()
    lane_counts: dict[str, int] = field(default_factory=dict)
    expected_lane_recall: float = 0.0
    expected_domains: tuple[str, ...] = ()
    observed_domains: tuple[str, ...] = ()
    missing_expected_domains: tuple[str, ...] = ()
    expected_domain_recall: float = 0.0
    forbidden_domains: tuple[str, ...] = ()
    forbidden_domain_hits: tuple[str, ...] = ()
    primary_source_count: int = 0
    useful_unique_domain_count: int = 0
    diagnosis: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_source_lanes(
    *,
    url: str = "",
    title: str = "",
    snippet: str = "",
    source_type: str = "",
) -> tuple[str, ...]:
    """Return source lanes represented by a URL/title/snippet packet."""

    domain = normalize_domain(url)
    lowered_url = str(url or "").lower()
    haystack = " ".join([lowered_url, title, snippet, source_type]).lower()
    lanes: list[str] = []

    if domain == "clinicaltrials.gov" or "clinical trial" in haystack or "nct0" in haystack:
        lanes.append("clinical_trials")
    if domain in GRANT_DOMAINS or any(
        term in haystack
        for term in ("grant", "funding opportunity", "sbir", "sttr", "nih reporter")
    ):
        lanes.append("grants_funding")
    if domain in LITERATURE_DOMAINS or any(
        term in haystack
        for term in ("pubmed", "pmid", "doi.org", "peer reviewed", "journal article")
    ):
        lanes.append("literature")
    if domain in PROCUREMENT_DOMAINS or any(
        term in haystack
        for term in ("rfp", "request for proposal", "solicitation", "sources sought")
    ):
        lanes.append("procurement_rfp")
    if any(domain == item or domain.endswith(f".{item}") for item in REGULATORY_DOMAINS):
        lanes.append("regulatory")
    if any(
        term in haystack
        for term in ("conference", "symposium", "summit", "workshop", "call for speakers")
    ):
        lanes.append("conference_events")
    if domain in PRESS_DOMAINS or any(
        term in haystack
        for term in ("press release", "announces", "raises", "series a", "seed round")
    ):
        lanes.append("press_news")
    if domain.endswith(".edu") or any(
        term in haystack
        for term in (
            "faculty",
            "principal investigator",
            "professor",
            "researcher",
            "institute",
            "medical school",
            "department of",
        )
    ):
        lanes.append("people_institutions")
    if domain in CAREERS_DOMAINS or any(
        term in haystack
        for term in ("/careers", "/jobs", "/job/", "/positions", "job posting", "apply now")
    ):
        lanes.append("careers_jobs")
    if _looks_like_company_site(domain=domain, lowered_url=lowered_url, haystack=haystack):
        lanes.append("company_site")

    return tuple(dict.fromkeys(lane for lane in lanes if lane in SOURCE_LANES))


def assess_source_coverage(
    results: Sequence[Any],
    *,
    expected_lanes: Sequence[str] = (),
    expected_domains: Sequence[str] = (),
    forbidden_domains: Sequence[str] = (),
) -> SourceCoverageAssessment:
    """Score expected source-lane and domain coverage for retrieved results."""

    normalized_expected_lanes = _normalize_lanes(expected_lanes)
    normalized_expected_domains = _normalize_domains(expected_domains)
    normalized_forbidden_domains = _normalize_domains(forbidden_domains)
    lane_counts: dict[str, int] = {}
    observed_domains: list[str] = []
    forbidden_hits: list[str] = []
    primary_source_count = 0

    for result in results:
        mapping = result_mapping(result)
        url = result_url(mapping)
        domain = normalize_domain(url)
        if domain:
            observed_domains.append(domain)
            if _domain_in(domain, normalized_forbidden_domains):
                forbidden_hits.append(domain)
        lanes = classify_source_lanes(
            url=url,
            title=str(mapping.get("title") or ""),
            snippet=str(mapping.get("snippet") or mapping.get("content") or ""),
            source_type=str(mapping.get("source_type") or mapping.get("source") or ""),
        )
        if any(lane in PRIMARY_SOURCE_LANES for lane in lanes):
            primary_source_count += 1
        for lane in lanes:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1

    observed_lanes = tuple(lane for lane in SOURCE_LANES if lane_counts.get(lane, 0) > 0)
    missing_lanes = tuple(lane for lane in normalized_expected_lanes if lane not in observed_lanes)
    observed_domain_tuple = tuple(dict.fromkeys(observed_domains))
    missing_domains = tuple(
        domain for domain in normalized_expected_domains if not _domain_in(domain, observed_domains)
    )
    diagnosis = _coverage_diagnosis(
        result_count=len(results),
        missing_lanes=missing_lanes,
        missing_domains=missing_domains,
        forbidden_hits=tuple(dict.fromkeys(forbidden_hits)),
        primary_source_count=primary_source_count,
    )
    return SourceCoverageAssessment(
        expected_lanes=normalized_expected_lanes,
        observed_lanes=observed_lanes,
        missing_lanes=missing_lanes,
        lane_counts=lane_counts,
        expected_lane_recall=_recall(
            observed=set(observed_lanes),
            expected=set(normalized_expected_lanes),
        ),
        expected_domains=normalized_expected_domains,
        observed_domains=observed_domain_tuple,
        missing_expected_domains=missing_domains,
        expected_domain_recall=_recall(
            observed=set(observed_domain_tuple),
            expected=set(normalized_expected_domains),
            domain_match=True,
        ),
        forbidden_domains=normalized_forbidden_domains,
        forbidden_domain_hits=tuple(dict.fromkeys(forbidden_hits)),
        primary_source_count=primary_source_count,
        useful_unique_domain_count=len(
            [
                domain
                for domain in observed_domain_tuple
                if domain and not _domain_in(domain, normalized_forbidden_domains)
            ]
        ),
        diagnosis=diagnosis,
    )


def required_source_lanes_for_company(
    *,
    company_url: str | None = None,
    request_text: str = "",
) -> tuple[str, ...]:
    """Return minimum useful source lanes for company/account research."""

    text = request_text.lower()
    lanes = ["company_site", "press_news"]
    if company_url:
        lanes.insert(0, "company_site")
    if any(term in text for term in ("career", "hiring", "job", "role")):
        lanes.append("careers_jobs")
    if any(term in text for term in ("clinical", "trial", "study", "validation")):
        lanes.extend(["clinical_trials", "literature"])
    if any(term in text for term in ("leadership", "founder", "executive", "team")):
        lanes.append("people_institutions")
    return tuple(dict.fromkeys(lanes))


def required_source_lanes_for_opportunity(
    *,
    request_text: str = "",
    target_entity_types: Sequence[str] = (),
    objectives: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return source lanes expected for an Opportunity Scout search packet."""

    text = " ".join([request_text, " ".join(target_entity_types), " ".join(objectives)]).lower()
    lanes: list[str] = []
    if any(term in text for term in ("broad", "company", "growth", "advisory", "partnership")):
        lanes.extend(["company_site", "press_news"])
    if any(term in text for term in ("role", "job", "hiring", "career")):
        lanes.append("careers_jobs")
    if any(term in text for term in ("trial", "clinical trial", "study")):
        lanes.append("clinical_trials")
    if any(term in text for term in ("grant", "funding", "sbir", "sttr")):
        lanes.append("grants_funding")
    if any(term in text for term in ("pubmed", "literature", "publication", "journal")):
        lanes.append("literature")
    if any(term in text for term in ("rfp", "contract", "procurement", "solicitation")):
        lanes.append("procurement_rfp")
    if any(term in text for term in ("conference", "presentation", "speaker", "workshop")):
        lanes.append("conference_events")
    if any(term in text for term in ("researcher", "institute", "university", "faculty")):
        lanes.append("people_institutions")
    if any(term in text for term in ("fda", "cms", "hhs", "regulatory")):
        lanes.append("regulatory")
    return tuple(dict.fromkeys(lanes))


def result_mapping(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(result, Mapping):
        return dict(result)
    return {
        "title": getattr(result, "title", ""),
        "url": getattr(result, "url", "") or getattr(result, "link", ""),
        "link": getattr(result, "link", ""),
        "snippet": getattr(result, "snippet", ""),
        "content": getattr(result, "content", ""),
        "source": getattr(result, "source", ""),
        "source_type": getattr(result, "source_type", ""),
    }


def result_url(mapping: Mapping[str, Any]) -> str:
    return str(mapping.get("url") or mapping.get("link") or "").strip()


def normalize_domain(url_or_domain: str) -> str:
    value = str(url_or_domain or "").strip().lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    domain = (parsed.netloc or parsed.path.split("/", 1)[0]).lower()
    return domain.removeprefix("www.")


def _looks_like_company_site(*, domain: str, lowered_url: str, haystack: str) -> bool:
    if not domain or domain in AGGREGATOR_DOMAINS:
        return False
    if domain.endswith((".gov", ".edu")) or domain in PRESS_DOMAINS or domain in CAREERS_DOMAINS:
        return False
    if any(path in lowered_url for path in COMPANY_SITE_PATH_HINTS):
        return True
    return any(term in haystack for term in ("company", "platform", "product", "partners"))


def _normalize_lanes(lanes: Sequence[str]) -> tuple[str, ...]:
    valid = set(SOURCE_LANES)
    return tuple(
        dict.fromkeys(
            normalized
            for lane in lanes
            if (normalized := str(lane or "").strip().lower()) in valid
        )
    )


def _normalize_domains(domains: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(domain for item in domains if (domain := normalize_domain(item))))


def _domain_in(domain: str, candidates: Sequence[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def _recall(
    *,
    observed: set[str],
    expected: set[str],
    domain_match: bool = False,
) -> float:
    if not expected:
        return 0.0
    if domain_match:
        hit_count = sum(1 for item in expected if _domain_in(item, tuple(observed)))
    else:
        hit_count = len(observed & expected)
    return round(hit_count / len(expected), 3)


def _coverage_diagnosis(
    *,
    result_count: int,
    missing_lanes: Sequence[str],
    missing_domains: Sequence[str],
    forbidden_hits: Sequence[str],
    primary_source_count: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if result_count == 0:
        reasons.append("provider returned no results")
    for lane in missing_lanes:
        reasons.append(f"provider did not return expected source lane: {lane}")
    for domain in missing_domains:
        reasons.append(f"provider did not return expected domain: {domain}")
    if forbidden_hits:
        reasons.append(f"forbidden/noisy domains surfaced: {', '.join(forbidden_hits)}")
    if result_count and primary_source_count == 0:
        reasons.append("no primary-source lanes found")
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "AGGREGATOR_DOMAINS",
    "CAREERS_DOMAINS",
    "COMPANY_SITE_PATH_HINTS",
    "GRANT_DOMAINS",
    "LITERATURE_DOMAINS",
    "PRESS_DOMAINS",
    "PRIMARY_SOURCE_LANES",
    "PROCUREMENT_DOMAINS",
    "REGULATORY_DOMAINS",
    "SOURCE_LANES",
    "SourceCoverageAssessment",
    "assess_source_coverage",
    "classify_source_lanes",
    "normalize_domain",
    "required_source_lanes_for_company",
    "required_source_lanes_for_opportunity",
    "result_mapping",
    "result_url",
]
