"""Deterministic fixture-mode company research."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from keystone_agents.guardrails import assess_unsupported_outreach_claims
from keystone_agents.schemas.company_profile import (
    DEFAULT_COMPANY_FEATURE_NAMES,
    DEFAULT_COMPARISON_CRITERIA,
    DEFAULT_RESEARCH_DATA_POINT_KEYS,
    ClaimEvidenceRecord,
    ClaimType,
    CompanyFeatureName,
    CompanyFeatureRecord,
    CompanyProfile,
    CompanyResearchComparison,
    CompanyResearchComparisonEntry,
    ComparisonCriterionKey,
    ComparisonOutcome,
    ResearchDataPoint,
    ResearchDataPointKey,
    SourceRecord,
    company_feature_label,
    research_data_point_label,
)
from keystone_agents.source_enrichment import (
    SourceBundle,
    build_source_bundle,
    dedupe_and_rank_source_records,
    detect_source_contradictions,
    extract_claim_candidates,
    extract_clean_text,
    source_missing_evidence,
    source_supports_text,
)
from keystone_agents.source_quality import (
    ResearchCompletenessScore,
    assess_research_completeness,
    score_source_quality,
    summarize_source_quality,
)

ALLOWED_SOURCE_TYPES = {
    "fixture",
    "academic",
    "company_site",
    "funding_database",
    "government",
    "website",
    "linkedin",
    "google_search",
    "news",
    "database",
    "social",
    "unknown",
    "user_provided",
}

COMPARISON_CRITERION_LABELS: dict[ComparisonCriterionKey, str] = {
    "consulting_fit": "Consulting fit",
    "evidence_strength": "Evidence strength",
    "evidence_generation_need": "Evidence generation need",
    "outside_consulting_likelihood": "Outside consulting likelihood",
    "clinical_relevance": "Clinical relevance",
}

COMPARISON_CRITERION_ALIASES = {
    "fit": "consulting_fit",
    "consulting fit": "consulting_fit",
    "consulting_fit": "consulting_fit",
    "evidence": "evidence_strength",
    "evidence strength": "evidence_strength",
    "evidence_strength": "evidence_strength",
    "confidence": "evidence_strength",
    "evidence generation": "evidence_generation_need",
    "evidence_generation": "evidence_generation_need",
    "evidence_generation_need": "evidence_generation_need",
    "outside consulting": "outside_consulting_likelihood",
    "outside_consulting": "outside_consulting_likelihood",
    "outside_consulting_likelihood": "outside_consulting_likelihood",
    "clinical relevance": "clinical_relevance",
    "clinical_relevance": "clinical_relevance",
}

OUTPUT_SECTION_ALIASES = {
    "summary": "summary",
    "evidence": "evidence",
    "concerns": "concerns",
    "next step": "next_step",
    "next_step": "next_step",
    "criteria": "criteria",
    "recommendation": "recommendation",
    "sources": "sources",
    "unknowns": "unknowns",
}

OUTPUT_SECTION_TITLES = {
    "summary": "Summary",
    "evidence": "Evidence",
    "concerns": "Concerns",
    "next_step": "Next Step",
    "criteria": "Criteria",
    "recommendation": "Recommendation",
    "sources": "Sources",
    "unknowns": "Unknowns",
}


@dataclass(frozen=True)
class ResearchSourceAggregation:
    """Normalized company research sources and claims excluded for safety."""

    sources: list[SourceRecord]
    unsupported_claims: list[str]


def _coerce_fixture(fixture_json: str | Path | dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    if fixture_json is None:
        return {}, "fixture://input"
    if isinstance(fixture_json, dict):
        return fixture_json, "fixture://inline"
    if isinstance(fixture_json, Path):
        text = fixture_json.read_text(encoding="utf-8")
        return json.loads(text), f"fixture://{fixture_json.name}"

    raw = str(fixture_json).strip()
    if not raw:
        return {}, "fixture://input"
    if raw.startswith("{"):
        return json.loads(raw), "fixture://inline"

    path = Path(raw)
    text = path.read_text(encoding="utf-8")
    return json.loads(text), f"fixture://{path.name}"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _as_research_data_point_key(value: Any) -> ResearchDataPointKey | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ai_relevance": "ai_data_science_relevance",
        "data_science_relevance": "ai_data_science_relevance",
        "growth_signal": "funding_growth_signal",
        "funding_signal": "funding_growth_signal",
        "research_signal": "research_signals",
        "fit": "consulting_fit",
    }
    normalized = aliases.get(text, text)
    if normalized in DEFAULT_RESEARCH_DATA_POINT_KEYS:
        return normalized  # type: ignore[return-value]
    return None


def _as_company_feature_name(value: Any) -> CompanyFeatureName | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "employee_count": "employee_count_range",
        "employees": "employee_count_range",
        "headcount": "employee_count_range",
        "segment": "market_segment",
        "clinical_relevance": "clinical_research_relevance",
        "research_relevance": "clinical_research_relevance",
        "ai": "ai_maturity",
        "ai_data_science_relevance": "ai_maturity",
        "data_assets": "data_asset_signal",
        "funding_signal": "funding_growth_signal",
        "growth_signal": "funding_growth_signal",
        "partnerships": "partnership_signal",
        "implementation": "implementation_complexity",
        "compliance": "compliance_sensitivity",
        "buyer": "buyer_function",
    }
    normalized = aliases.get(text, text)
    if normalized in DEFAULT_COMPANY_FEATURE_NAMES:
        return normalized  # type: ignore[return-value]
    return None


def _safe_source_type(value: Any, *, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "company_website": "company_site",
        "company": "company_site",
        "profile": "user_provided",
        "profile_like": "user_provided",
        "search": "google_search",
        "serper": "google_search",
        "searxng": "google_search",
        "metasearch": "google_search",
        "web": "website",
    }
    normalized = aliases.get(text, text)
    if normalized in ALLOWED_SOURCE_TYPES:
        return normalized
    return default


def _coerce_payload(raw_value: Any) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if isinstance(raw_value, BaseModel):
        return raw_value.model_dump(mode="json")
    if isinstance(raw_value, dict):
        return dict(raw_value)
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
            return loaded if isinstance(loaded, dict) else {"items": loaded}
        return {"text": text}
    return {
        key: getattr(raw_value, key)
        for key in ("title", "url", "link", "snippet", "source", "text_or_markdown", "claims")
        if hasattr(raw_value, key)
    }


def _coerce_payloads(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [_coerce_payload(item) for item in value]
    return [_coerce_payload(value)]


def _payload_claims(payload: dict[str, Any], *, company_name: str = "") -> list[str]:
    for key in (
        "supported_claims",
        "claims",
        "facts",
        "signals",
        "quote_or_fact",
        "fact",
    ):
        claims = _as_list(payload.get(key))
        if claims:
            return claims
    for key in ("summary", "description", "content", "text_or_markdown", "text", "html"):
        raw_text = str(payload.get(key) or "").strip()
        if not raw_text:
            continue
        extracted = extract_claim_candidates(raw_text, company_name=company_name)
        if extracted:
            return extracted
        clean_text = extract_clean_text(raw_text)
        if clean_text and len(clean_text) <= 280:
            return [clean_text]
    return []


def _payload_evidence_excerpt(payload: dict[str, Any], *, max_chars: int = 900) -> str:
    for key in ("evidence_excerpt", "text_or_markdown", "content", "text", "html", "summary"):
        raw_text = str(payload.get(key) or "").strip()
        if not raw_text:
            continue
        clean_text = extract_clean_text(raw_text)
        if not clean_text:
            continue
        return clean_text[:max_chars].rstrip()
    return ""


def _unsupported_claim_flags(claim: str) -> list[str]:
    return [
        f"unsupported company research claim: {flag}"
        for flag in assess_unsupported_outreach_claims(claim)
    ]


def _filter_supported_claims(claims: list[str]) -> tuple[list[str], list[str]]:
    safe_claims: list[str] = []
    unsupported: list[str] = []
    for claim in claims:
        flags = _unsupported_claim_flags(claim)
        if flags:
            unsupported.extend(flags)
            continue
        safe_claims.append(claim)
    return list(dict.fromkeys(safe_claims)), list(dict.fromkeys(unsupported))


def _fixture_claims(data: dict[str, Any]) -> list[str]:
    claims: list[str] = []
    for key in ("description", "fit", "summary", "business_summary", "notes"):
        claims.extend(_as_list(data.get(key)))
    claims.extend(_as_list(data.get("evidence")))
    claims.extend(_as_list(data.get("signals")))
    return list(dict.fromkeys(claims))


def _claim_type_for_text(claim: str) -> ClaimType:
    lowered = claim.lower()
    if "fixture input identifies" in lowered or "company as" in lowered:
        return "company_identity"
    if "fit" in lowered or "relevant" in lowered or "keystone" in lowered:
        return "company_fit"
    if any(
        term in lowered
        for term in (
            "funding",
            "hiring",
            "partnership",
            "pilot",
            "trial",
            "validation",
            "outcomes",
            "conference",
            "publication",
        )
    ):
        return "company_signal"
    return "company_description"


def _normalize_claim_type(value: Any, default: ClaimType) -> ClaimType:
    allowed = set(ClaimType.__args__)
    text = str(value or "").strip()
    return text if text in allowed else default  # type: ignore[return-value]


def _source_records(
    data: dict[str, Any],
    *,
    company_name: str,
    company_url: str | None,
    fixture_ref: str,
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for index, raw_source in enumerate(data.get("sources") or [], start=1):
        if not isinstance(raw_source, dict):
            continue
        claims = _payload_claims(raw_source, company_name=company_name)
        if not claims:
            continue
        records.append(
            SourceRecord(
                source_id=str(
                    raw_source.get("source_id") or raw_source.get("id") or f"source-{index}"
                ),
                title=str(raw_source.get("title") or f"{company_name} source {index}"),
                url=str(raw_source.get("url") or fixture_ref),
                source_type=_safe_source_type(raw_source.get("source_type"), default="fixture"),
                supported_claims=claims,
                evidence_excerpt=_payload_evidence_excerpt(raw_source),
                confidence=float(raw_source.get("confidence", 0.75)),
                published_at=raw_source.get("published_at") or raw_source.get("date"),
            )
        )

    if records:
        return records

    claims = _fixture_claims(data)
    if not claims:
        claims = [f"Fixture input identifies the company as {company_name}."]

    source_key = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-") or "company"
    return [
        SourceRecord(
            source_id=f"fixture:{source_key}",
            title=f"Fixture record for {company_name}",
            url=fixture_ref if fixture_ref != "fixture://input" else company_url or fixture_ref,
            source_type="fixture",
            supported_claims=claims,
            confidence=0.7 if data else 0.45,
        )
    ]


def _claim_records(
    data: dict[str, Any],
    sources: list[SourceRecord],
) -> list[ClaimEvidenceRecord]:
    records: list[ClaimEvidenceRecord] = []
    raw_records = (
        data.get("claims") or data.get("claim_evidence") or data.get("evidence_records") or []
    )
    if isinstance(raw_records, list):
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            claim_text = str(
                raw_record.get("claim_text")
                or raw_record.get("claim")
                or raw_record.get("text")
                or ""
            ).strip()
            source_id = str(raw_record.get("source_id") or "").strip()
            if not claim_text or not source_id:
                continue
            records.append(
                ClaimEvidenceRecord(
                    claim_text=claim_text,
                    source_id=source_id,
                    confidence=float(raw_record.get("confidence", 0.7)),
                    claim_type=_normalize_claim_type(
                        raw_record.get("claim_type"),
                        _claim_type_for_text(claim_text),
                    ),
                )
            )

    existing = {(record.source_id, record.claim_text) for record in records}
    for source in sources:
        for claim in source.supported_claims:
            key = (source.source_id, claim)
            if key in existing:
                continue
            records.append(
                ClaimEvidenceRecord(
                    claim_text=claim,
                    source_id=source.source_id,
                    confidence=source.confidence,
                    claim_type=_claim_type_for_text(claim),
                )
            )
            existing.add(key)
    return records


def _filter_claim_records(
    claims: list[ClaimEvidenceRecord],
) -> tuple[list[ClaimEvidenceRecord], list[str]]:
    safe_claims: list[ClaimEvidenceRecord] = []
    unsupported: list[str] = []
    for claim in claims:
        flags = _unsupported_claim_flags(claim.claim_text)
        if flags:
            unsupported.extend(flags)
            continue
        safe_claims.append(claim)
    return safe_claims, list(dict.fromkeys(unsupported))


def _filter_source_backed_claim_records(
    claims: list[ClaimEvidenceRecord],
    sources: list[SourceRecord],
) -> tuple[list[ClaimEvidenceRecord], list[str]]:
    source_by_id = {source.source_id: source for source in sources}
    backed: list[ClaimEvidenceRecord] = []
    unsupported: list[str] = []
    for claim in claims:
        if source_supports_text(
            text=claim.claim_text,
            source_ids=[claim.source_id],
            source_by_id=source_by_id,
        ):
            backed.append(claim)
        else:
            unsupported.append(f"claim not supported by source record: {claim.claim_text}")
    return backed, list(dict.fromkeys(unsupported))


def _search_source_records(search_results: list[Any] | None) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for index, result in enumerate(search_results or [], start=1):
        if isinstance(result, dict):
            title = str(result.get("title") or "").strip()
            link = str(result.get("link") or result.get("url") or "").strip()
            snippet = str(result.get("snippet") or "").strip()
            source = str(result.get("source") or "serper").strip()
            source_type = _safe_source_type(result.get("source_type"), default="google_search")
            published_at = result.get("published_at") or result.get("date")
        else:
            title = str(getattr(result, "title", "")).strip()
            link = str(getattr(result, "link", "")).strip()
            snippet = str(getattr(result, "snippet", "")).strip()
            source = str(getattr(result, "source", "serper")).strip()
            source_type = "google_search"
            published_at = getattr(result, "published_at", None)
        if not title or not link:
            continue
        claim = snippet or title
        records.append(
            SourceRecord(
                source_id=f"{source or 'serper'}:{index}",
                title=title,
                url=link,
                source_type=source_type,
                supported_claims=[claim],
                confidence=0.65,
                published_at=published_at,
            )
        )
    return records


def _website_source_records(
    website_inputs: Any,
    *,
    company_name: str,
    company_url: str | None,
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for index, payload in enumerate(_coerce_payloads(website_inputs), start=1):
        claims = _payload_claims(payload, company_name=company_name)
        if not claims:
            continue
        url = str(
            payload.get("url") or payload.get("company_url") or company_url or "fixture://website"
        ).strip()
        title = str(payload.get("title") or f"{company_name} website source").strip()
        records.append(
            SourceRecord(
                source_id=str(payload.get("source_id") or f"website:{index}"),
                title=title,
                url=url,
                source_type=_safe_source_type(payload.get("source_type"), default="website"),
                supported_claims=claims,
                evidence_excerpt=_payload_evidence_excerpt(payload),
                confidence=float(payload.get("confidence", 0.7)),
                published_at=payload.get("published_at") or payload.get("date"),
            )
        )
    return records


def _profile_source_records(
    profile_inputs: Any,
    *,
    company_name: str,
    linkedin_url: str | None,
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for index, payload in enumerate(_coerce_payloads(profile_inputs), start=1):
        if payload.get("placeholder") and not _payload_claims(
            payload,
            company_name=company_name,
        ):
            continue
        claims = _payload_claims(payload, company_name=company_name)
        if not claims:
            continue
        url = str(
            payload.get("linkedin_url") or payload.get("profile_url") or payload.get("url") or ""
        )
        url = url.strip() or linkedin_url or "fixture://profile"
        source_type = "linkedin" if "linkedin.com" in url.lower() else "user_provided"
        title = str(payload.get("title") or f"{company_name} profile source").strip()
        records.append(
            SourceRecord(
                source_id=str(payload.get("source_id") or f"profile:{index}"),
                title=title,
                url=url,
                source_type=_safe_source_type(payload.get("source_type"), default=source_type),
                supported_claims=claims,
                evidence_excerpt=_payload_evidence_excerpt(payload),
                confidence=float(payload.get("confidence", 0.6)),
                published_at=payload.get("published_at") or payload.get("date"),
            )
        )
    return records


def _dedupe_sources(sources: list[SourceRecord]) -> list[SourceRecord]:
    deduped: list[SourceRecord] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    used_ids: set[str] = set()
    for source in sources:
        key = (
            source.url.lower(),
            tuple(sorted(claim.lower() for claim in source.supported_claims)),
        )
        if key in seen:
            continue
        seen.add(key)
        source_id = source.source_id
        if source_id in used_ids:
            source_id = f"{source.source_id}:{len(used_ids) + 1}"
            source = source.model_copy(update={"source_id": source_id})
        used_ids.add(source_id)
        deduped.append(source)
    return deduped


COMPANY_FEATURE_TERMS: dict[CompanyFeatureName, tuple[str, ...]] = {
    "employee_count_range": ("employees", "employee count", "headcount", "team size"),
    "market_segment": (
        "behavioral health",
        "digital health",
        "clinical research",
        "pharma",
        "biotech",
        "provider",
        "payer",
        "health system",
        "life sciences",
        "neurotechnology",
    ),
    "clinical_research_relevance": (
        "clinical research",
        "clinical trial",
        "trial",
        "evidence generation",
        "validation",
        "outcomes",
        "study",
        "publication",
    ),
    "ai_maturity": (
        "ai",
        "artificial intelligence",
        "machine learning",
        "analytics",
        "automation",
        "data science",
        "model",
    ),
    "data_asset_signal": (
        "data",
        "dataset",
        "real-world evidence",
        "outcomes",
        "registry",
        "ehr",
        "claims",
        "patient-reported",
        "measurement",
    ),
    "funding_growth_signal": (
        "funding",
        "series",
        "seed",
        "venture",
        "hiring",
        "growth",
        "launch",
        "scaling",
    ),
    "partnership_signal": (
        "partnership",
        "partner",
        "collaboration",
        "alliance",
        "payer",
        "provider",
        "pharma",
        "sponsor",
    ),
    "implementation_complexity": (
        "implementation",
        "integration",
        "workflow",
        "enterprise",
        "deployment",
        "operations",
        "clinical operations",
    ),
    "compliance_sensitivity": (
        "hipaa",
        "compliance",
        "regulated",
        "privacy",
        "security",
        "clinical",
        "patient",
        "health data",
    ),
    "buyer_function": (
        "buyer",
        "clinical operations",
        "research operations",
        "medical affairs",
        "product",
        "data science",
        "commercial",
        "partnerships",
        "provider",
        "payer",
    ),
}


def _feature_value_from_claim(
    feature_name: CompanyFeatureName,
    claim: str,
) -> str:
    if feature_name == "employee_count_range":
        employee_match = re.search(
            r"\b\d{1,6}\s*(?:-|to)\s*\d{1,6}\s+employees\b|\b\d{1,6}\+?\s+employees\b",
            claim,
            flags=re.I,
        )
        if employee_match:
            return employee_match.group(0)
    lowered = claim.lower()
    for term in COMPANY_FEATURE_TERMS[feature_name]:
        if term in lowered:
            return term
    return company_feature_label(feature_name)


def _coerce_company_features(
    data: dict[str, Any],
) -> list[CompanyFeatureRecord]:
    raw_features = data.get("company_features") or data.get("features") or []
    if isinstance(raw_features, dict):
        raw_items = []
        for key, value in raw_features.items():
            item = dict(value) if isinstance(value, dict) else {"value": value}
            item.setdefault("feature_name", key)
            raw_items.append(item)
    elif isinstance(raw_features, list):
        raw_items = raw_features
    else:
        return []

    features: list[CompanyFeatureRecord] = []
    for raw_feature in raw_items:
        if not isinstance(raw_feature, dict):
            continue
        feature_name = _as_company_feature_name(
            raw_feature.get("feature_name") or raw_feature.get("name") or raw_feature.get("key")
        )
        if feature_name is None:
            continue
        source_id = str(
            raw_feature.get("source_id") or next(iter(_as_list(raw_feature.get("source_ids"))), "")
        ).strip()
        evidence_text = str(
            raw_feature.get("evidence_text")
            or raw_feature.get("evidence")
            or raw_feature.get("claim")
            or ""
        ).strip()
        value = str(raw_feature.get("value") or raw_feature.get(feature_name) or "").strip()
        if not value:
            value = evidence_text
        if not source_id or not evidence_text or not value:
            continue
        features.append(
            CompanyFeatureRecord(
                feature_name=feature_name,
                value=value,
                source_id=source_id,
                evidence_text=evidence_text,
                confidence=float(raw_feature.get("confidence", 0.5)),
                approved=bool(raw_feature.get("approved", True)),
            )
        )
    return features


def _inferred_company_features(sources: list[SourceRecord]) -> list[CompanyFeatureRecord]:
    features_by_name: dict[CompanyFeatureName, CompanyFeatureRecord] = {}
    for source in sources:
        for claim in source.supported_claims:
            lowered_claim = claim.lower()
            for feature_name, terms in COMPANY_FEATURE_TERMS.items():
                if feature_name in features_by_name:
                    continue
                if not any(term in lowered_claim for term in terms):
                    continue
                features_by_name[feature_name] = CompanyFeatureRecord(
                    feature_name=feature_name,
                    value=_feature_value_from_claim(feature_name, claim),
                    source_id=source.source_id,
                    evidence_text=claim,
                    confidence=source.confidence,
                )
    return [
        features_by_name[name] for name in DEFAULT_COMPANY_FEATURE_NAMES if name in features_by_name
    ]


def _source_backed_company_features(
    features: list[CompanyFeatureRecord],
    sources: list[SourceRecord],
) -> list[CompanyFeatureRecord]:
    source_by_id = {source.source_id: source for source in sources}
    return [feature for feature in features if feature.is_source_backed(source_by_id)]


def _merge_company_features(
    explicit_features: list[CompanyFeatureRecord],
    inferred_features: list[CompanyFeatureRecord],
    sources: list[SourceRecord],
) -> list[CompanyFeatureRecord]:
    source_by_id = {source.source_id: source for source in sources}
    by_name: dict[CompanyFeatureName, CompanyFeatureRecord] = {
        feature.feature_name: feature for feature in inferred_features
    }
    for feature in explicit_features:
        existing = by_name.get(feature.feature_name)
        if feature.has_source_support(source_by_id) or existing is None:
            by_name[feature.feature_name] = feature
    return [by_name[name] for name in DEFAULT_COMPANY_FEATURE_NAMES if name in by_name]


def _company_feature_text(features: list[CompanyFeatureRecord]) -> str:
    return " ".join(
        f"{company_feature_label(feature.feature_name)}: {feature.value}. "
        f"Evidence: {feature.evidence_text}."
        for feature in features
    )


def _source_ids_for_terms(
    *,
    sources: list[SourceRecord],
    terms: tuple[str, ...],
) -> list[str]:
    source_ids: list[str] = []
    for source in sources:
        text = " ".join([source.title, source.url, *source.supported_claims]).lower()
        if any(term in text for term in terms):
            source_ids.append(source.source_id)
    return list(dict.fromkeys(source_ids))


def _best_claim_for_terms(
    *,
    claims: list[str],
    terms: tuple[str, ...],
) -> str:
    for claim in claims:
        lowered = claim.lower()
        if any(term in lowered for term in terms):
            return claim
    return ""


def _data_point_confidence(
    *,
    source_ids: list[str],
    source_by_id: dict[str, SourceRecord],
    source_quality_summary_score: int,
) -> float:
    if not source_ids:
        return 0.0
    source_confidences = [
        source_by_id[source_id].confidence for source_id in source_ids if source_id in source_by_id
    ]
    average_source_confidence = (
        sum(source_confidences) / len(source_confidences) if source_confidences else 0.5
    )
    corroboration_bonus = min(len(source_ids) - 1, 2) * 0.07
    quality_component = source_quality_summary_score / 100 * 0.25
    value = average_source_confidence * 0.68 + quality_component + corroboration_bonus
    return round(min(value, 0.95), 2)


def _build_research_data_point(
    *,
    key: ResearchDataPointKey,
    value: str,
    source_ids: list[str],
    source_by_id: dict[str, SourceRecord],
    source_quality_summary_score: int,
    missing_reason: str,
    confidence: float | None = None,
    rationale: str = "",
) -> ResearchDataPoint:
    completed = bool(value.strip() and source_ids)
    return ResearchDataPoint(
        key=key,
        label=research_data_point_label(key),
        value=value,
        completed=completed,
        confidence=(
            confidence
            if confidence is not None
            else _data_point_confidence(
                source_ids=source_ids,
                source_by_id=source_by_id,
                source_quality_summary_score=source_quality_summary_score,
            )
        ),
        source_ids=source_ids,
        rationale=rationale,
        missing_reason="" if completed else missing_reason,
    )


def _coerce_research_data_points(
    data: dict[str, Any],
    *,
    sources: list[SourceRecord],
    source_quality_summary_score: int,
) -> list[ResearchDataPoint]:
    raw_points = data.get("research_data_points") or data.get("data_points") or []
    if not isinstance(raw_points, list):
        return []
    source_by_id = {source.source_id: source for source in sources}
    points: list[ResearchDataPoint] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, dict):
            continue
        key = _as_research_data_point_key(raw_point.get("key") or raw_point.get("name"))
        if key is None:
            continue
        source_ids = [
            source_id
            for source_id in _as_list(raw_point.get("source_ids") or raw_point.get("sources"))
            if source_id in source_by_id
        ]
        value = str(raw_point.get("value") or raw_point.get("answer") or "").strip()
        completed = raw_point.get("completed")
        if completed is False:
            value = ""
            source_ids = []
        elif (
            value
            and source_ids
            and not source_supports_text(
                text=value,
                source_ids=source_ids,
                source_by_id=source_by_id,
            )
        ):
            value = ""
            source_ids = []
        confidence = raw_point.get("confidence")
        points.append(
            ResearchDataPoint(
                key=key,
                label=str(raw_point.get("label") or research_data_point_label(key)),
                value=value,
                completed=bool(completed if completed is not None else value and source_ids),
                confidence=(
                    float(confidence)
                    if confidence is not None
                    else _data_point_confidence(
                        source_ids=source_ids,
                        source_by_id=source_by_id,
                        source_quality_summary_score=source_quality_summary_score,
                    )
                ),
                source_ids=source_ids,
                rationale=str(raw_point.get("rationale") or ""),
                missing_reason=str(
                    raw_point.get("missing_reason")
                    or (
                        "Research data point value was not supported by the cited source."
                        if not value
                        else ""
                    )
                ),
            )
        )
    return points


def _inferred_research_data_points(
    *,
    data: dict[str, Any],
    sources: list[SourceRecord],
    evidence: list[str],
    scores: dict[str, int],
    fit_summary: str,
    source_quality_summary_score: int,
) -> list[ResearchDataPoint]:
    source_by_id = {source.source_id: source for source in sources}
    all_claim_text = " ".join(evidence).lower()

    def point(
        key: ResearchDataPointKey,
        *,
        terms: tuple[str, ...],
        value: str = "",
        missing_reason: str,
        min_score: int | None = None,
        rationale: str = "",
    ) -> ResearchDataPoint:
        source_ids = _source_ids_for_terms(sources=sources, terms=terms)
        resolved_value = value or _best_claim_for_terms(claims=evidence, terms=terms)
        if min_score is not None and not resolved_value and source_ids:
            resolved_value = f"Score signal: {min_score}/100."
        if min_score is not None and min_score < 35 and not source_ids:
            resolved_value = ""
        if resolved_value and not source_ids:
            lowered_value = resolved_value.lower()
            matched_terms = tuple(term for term in terms if term in lowered_value)
            if matched_terms:
                source_ids = _source_ids_for_terms(sources=sources, terms=matched_terms)
        return _build_research_data_point(
            key=key,
            value=resolved_value,
            source_ids=source_ids,
            source_by_id=source_by_id,
            source_quality_summary_score=source_quality_summary_score,
            missing_reason=missing_reason,
            rationale=rationale,
        )

    explicit_fit = str(data.get("fit") or fit_summary or "")
    points = [
        point(
            "business_model",
            terms=(
                "business model",
                "software",
                "platform",
                "service",
                "clinical trial",
                "digital health",
                "technology",
            ),
            value=str(data.get("business_model") or ""),
            missing_reason="Business model not confirmed by source-backed evidence.",
        ),
        point(
            "customer_segment",
            terms=(
                "customer",
                "buyer",
                "payer",
                "provider",
                "employer",
                "trial",
                "sponsor",
                "patient",
                "health system",
            ),
            value=str(data.get("customer_segment") or ""),
            missing_reason="Customer segment or buyer context not confirmed.",
        ),
        point(
            "behavioral_health_relevance",
            terms=(
                "behavioral health",
                "mental health",
                "psychiatry",
                "therapy",
                "care navigation",
                "measurement based care",
                "depression",
                "anxiety",
            ),
            value=(
                f"Behavioral health relevance score: {scores['behavioral_health_relevance']}/100."
                if scores["behavioral_health_relevance"] >= 35
                else ""
            ),
            min_score=scores["behavioral_health_relevance"],
            missing_reason="Behavioral health relevance not confirmed.",
        ),
        point(
            "ai_data_science_relevance",
            terms=(
                "ai",
                "artificial intelligence",
                "analytics",
                "data science",
                "automation",
                "workflow",
                "clinical ai",
                "software",
            ),
            value=(
                f"AI/data science relevance score: {scores['clinical_ai_relevance']}/100."
                if scores["clinical_ai_relevance"] >= 35
                else ""
            ),
            min_score=scores["clinical_ai_relevance"],
            missing_reason="AI or data science relevance not confirmed.",
        ),
        point(
            "research_signals",
            terms=(
                "research",
                "trial",
                "validation",
                "outcomes",
                "evidence",
                "publication",
                "study",
                "clinical",
            ),
            value=str(data.get("research_signals") or ""),
            missing_reason="Research, evidence, or validation signals not confirmed.",
        ),
        point(
            "funding_growth_signal",
            terms=(
                "funding",
                "hiring",
                "partnership",
                "launch",
                "scaling",
                "growth",
                "conference",
            ),
            value=str(data.get("funding_growth_signal") or ""),
            missing_reason="Funding, growth, launch, or hiring signal not confirmed.",
        ),
        point(
            "compliance_sensitivity",
            terms=(
                "hipaa",
                "compliance",
                "regulated",
                "clinical",
                "patient",
                "healthcare",
                "trial",
                "data",
            ),
            value=str(data.get("compliance_sensitivity") or ""),
            missing_reason="Compliance sensitivity not confirmed.",
        ),
        point(
            "consulting_fit",
            terms=(
                "keystone",
                "fit",
                "relevant",
                "behavioral health",
                "clinical trial",
                "evidence generation",
                "clinical ai",
            ),
            value=explicit_fit,
            min_score=scores["consulting_fit_score"],
            missing_reason="Consulting fit could not be confirmed from source-backed evidence.",
        ),
    ]

    if "compliance" in all_claim_text or "hipaa" in all_claim_text:
        return points
    return points


def _merge_research_data_points(
    explicit_points: list[ResearchDataPoint],
    inferred_points: list[ResearchDataPoint],
) -> list[ResearchDataPoint]:
    by_key: dict[ResearchDataPointKey, ResearchDataPoint] = {}
    for point in inferred_points:
        by_key[point.key] = point
    for point in explicit_points:
        existing = by_key.get(point.key)
        if existing is None or point.completed or not existing.completed:
            by_key[point.key] = point
    return [by_key[key] for key in DEFAULT_RESEARCH_DATA_POINT_KEYS]


def aggregate_research_sources(
    *,
    data: dict[str, Any],
    company_name: str,
    company_url: str | None,
    linkedin_url: str | None,
    fixture_ref: str,
    search_results: list[Any] | None = None,
    website_inputs: Any = None,
    profile_inputs: Any = None,
) -> ResearchSourceAggregation:
    """Aggregate fixture, search, website, and profile-like inputs into source records."""

    search_sources = _search_source_records(search_results)
    website_sources = _website_source_records(
        website_inputs or data.get("website_inputs") or data.get("website_sources"),
        company_name=company_name,
        company_url=company_url,
    )
    profile_sources = _profile_source_records(
        profile_inputs or data.get("profile_inputs") or data.get("profile_sources"),
        company_name=company_name,
        linkedin_url=linkedin_url,
    )
    external_sources = [*search_sources, *website_sources, *profile_sources]
    fixture_sources = (
        _source_records(
            data,
            company_name=company_name,
            company_url=company_url,
            fixture_ref=fixture_ref,
        )
        if data or not external_sources
        else []
    )
    raw_sources = [*fixture_sources, *external_sources]

    sources: list[SourceRecord] = []
    unsupported: list[str] = []
    for source in raw_sources:
        safe_claims, unsupported_claims = _filter_supported_claims(source.supported_claims)
        unsupported.extend(unsupported_claims)
        if not safe_claims:
            continue
        sources.append(source.model_copy(update={"supported_claims": safe_claims}))

    return ResearchSourceAggregation(
        sources=_dedupe_sources(sources),
        unsupported_claims=list(dict.fromkeys(unsupported)),
    )


def _score_sources(
    sources: list[SourceRecord],
    *,
    company_url: str | None,
) -> list[SourceRecord]:
    scored: list[SourceRecord] = []
    for source in sources:
        source_quality = score_source_quality(
            url=source.url,
            title=source.title,
            declared_source_type=source.source_type,
            supported_text=" ".join(source.supported_claims),
            company_url=company_url,
            published_at=source.published_at,
            claim_context="company_fact",
        )
        scored.append(
            source.model_copy(
                update={
                    "confidence": round(source_quality.overall_score / 100, 2),
                    "source_quality": source_quality,
                }
            )
        )
    return scored


def _rank_sources_for_request_focus(
    sources: list[SourceRecord],
    *,
    request_focus_terms: list[str] | None,
) -> list[SourceRecord]:
    """Prefer sources whose title, URL, claims, or extracted text match the request focus."""

    focus_terms = [
        str(term or "").strip().lower()
        for term in request_focus_terms or []
        if str(term or "").strip()
    ]
    if not focus_terms:
        return sources

    scored: list[tuple[int, int, SourceRecord]] = []
    for position, source in enumerate(sources):
        url = source.url.lower()
        title = source.title.lower()
        claims = " ".join(source.supported_claims).lower()
        excerpt = source.evidence_excerpt.lower()
        haystack = f"{url} {title} {claims} {excerpt}"
        score = 0
        for term in focus_terms:
            if term in url:
                score += 4
            if term in title:
                score += 3
            if term in claims:
                score += 2
            if term in excerpt:
                score += 2
            if term in haystack:
                score += 1
        if score and source.evidence_excerpt.strip():
            score += 2
        scored.append((score, position, source))

    if not any(score for score, _position, _source in scored):
        return sources
    return [
        source for score, position, source in sorted(scored, key=lambda item: (-item[0], item[1]))
    ]


def _evidence_from_sources(sources: list[SourceRecord]) -> list[str]:
    return list(
        dict.fromkeys(
            claim for source in sources for claim in source.supported_claims if claim.strip()
        )
    )


def _quality_scores_from_sources(sources: list[SourceRecord]) -> list[Any]:
    return [source.source_quality for source in sources if source.source_quality is not None]


def _select_sources_until_complete(
    sources: list[SourceRecord],
    *,
    unsupported_claims: list[str],
) -> tuple[list[SourceRecord], ResearchCompletenessScore]:
    """Keep fixture/mock evidence only until research is complete enough."""

    selected: list[SourceRecord] = []
    completeness = assess_research_completeness(
        source_scores=[],
        evidence=[],
        unsupported_claims=unsupported_claims,
    )
    for source in sources:
        selected.append(source)
        completeness = assess_research_completeness(
            source_scores=_quality_scores_from_sources(selected),
            evidence=_evidence_from_sources(selected),
            unsupported_claims=unsupported_claims,
        )
        if completeness.stop_recommended:
            return selected, completeness
    return selected, completeness


def _score(text: str, primary: tuple[str, ...], secondary: tuple[str, ...] = ()) -> int:
    primary_hits = [term for term in primary if term in text]
    secondary_hits = [term for term in secondary if term in text]
    return min(100, len(primary_hits) * 35 + len(secondary_hits) * 15)


def _score_profile(text: str) -> dict[str, int]:
    behavioral = _score(
        text,
        (
            "behavioral health",
            "mental health",
            "psychiatry",
            "therapy",
            "care navigation",
            "measurement based care",
        ),
        (
            "patient engagement",
            "population health",
            "wellbeing",
            "wellness",
            "depression",
            "anxiety",
        ),
    )
    if "behavioral health" in text:
        behavioral = min(100, behavioral + 25)
    clinical_ai = _score(
        text,
        (
            "clinical ai",
            "ai-assisted",
            "clinical operations",
            "clinical trial",
            "trial software",
            "trial-tech",
            "decentralized clinical trial",
        ),
        ("analytics", "software", "digital health", "automation", "workflow", "clinical research"),
    )
    cns_neuro = _score(
        text,
        ("neuroscience", "neuroinformatics", "neurotechnology", "neurology", "cns", "brain"),
        ("neuro", "cognitive", "psychiatric"),
    )
    evidence = _score(
        text,
        (
            "evidence generation",
            "clinical trial",
            "trial",
            "validation",
            "outcomes",
            "real-world evidence",
            "publication",
        ),
        ("research", "pilot", "study", "clinical", "payer", "measure"),
    )
    outside = _score(
        text,
        (
            "funding",
            "hiring",
            "partnership",
            "pilot",
            "validation",
            "payer",
            "conference",
            "publication",
            "scaling",
            "consulting",
        ),
        ("operations", "workflow", "trial", "research", "go-to-market"),
    )

    ranked_domains = sorted([behavioral, clinical_ai, cns_neuro, evidence], reverse=True)
    consulting_fit = round(
        ranked_domains[0] * 0.45 + ranked_domains[1] * 0.15 + evidence * 0.25 + outside * 0.15
    )
    if behavioral >= 80:
        consulting_fit += 25
    if clinical_ai >= 70 and evidence >= 70:
        consulting_fit += 10

    return {
        "behavioral_health_relevance": behavioral,
        "clinical_ai_relevance": clinical_ai,
        "cns_neuro_relevance": cns_neuro,
        "evidence_generation_need": evidence,
        "outside_consulting_likelihood": outside,
        "consulting_fit_score": min(100, consulting_fit),
    }


def _confidence(
    sources: list[SourceRecord],
    evidence_count: int,
    *,
    data_points: list[ResearchDataPoint],
    features: list[CompanyFeatureRecord],
    source_quality_score: int,
    research_completeness_score: int,
    independent_source_count: int,
    low_quality_source_count: int,
    has_website: bool,
    has_linkedin: bool,
    unsupported_claim_count: int,
    contradiction_count: int = 0,
) -> float:
    if not sources:
        return 0.2
    value = (
        0.15
        + (source_quality_score / 100) * 0.55
        + min(independent_source_count, 4) * 0.05
        + min(len(sources), 5) * 0.025
        + min(evidence_count, 8) * 0.025
    )
    completed_data_points = [data_point for data_point in data_points if data_point.completed]
    source_backed_coverage = len(completed_data_points) / len(DEFAULT_RESEARCH_DATA_POINT_KEYS)
    average_data_point_confidence = (
        sum(data_point.confidence for data_point in completed_data_points)
        / len(completed_data_points)
        if completed_data_points
        else 0.0
    )
    value += source_backed_coverage * 0.12 + average_data_point_confidence * 0.08
    unique_feature_names = {feature.feature_name for feature in features}
    feature_coverage = len(unique_feature_names) / len(DEFAULT_COMPANY_FEATURE_NAMES)
    average_feature_confidence = (
        sum(feature.confidence for feature in features) / len(features) if features else 0.0
    )
    value += feature_coverage * 0.06 + average_feature_confidence * 0.04
    value += (research_completeness_score / 100) * 0.08
    if source_backed_coverage < 0.5:
        value -= 0.1
    if independent_source_count < 2:
        value -= 0.32
    if len(sources) == 1:
        value -= 0.1
    if research_completeness_score < 50:
        value -= 0.08
    if has_website:
        value += 0.05
    if has_linkedin:
        value += 0.03
    if low_quality_source_count >= len(sources):
        value -= 0.08
    if unsupported_claim_count:
        value -= min(unsupported_claim_count, 3) * 0.04
    if contradiction_count:
        value -= min(contradiction_count, 3) * 0.1
        value = min(value, 0.88)
    return round(max(0.0, min(value, 0.95)), 2)


def _confidence_explanation(
    *,
    confidence_score: float,
    source_quality_score: int,
    research_completeness: ResearchCompletenessScore,
    independent_source_count: int,
    low_quality_source_count: int,
    unsupported_claim_count: int,
    contradiction_count: int = 0,
) -> str:
    notes = [
        f"confidence={confidence_score:.2f}",
        f"source_quality={source_quality_score}/100",
        f"research_completeness={research_completeness.score}/100",
        f"independent_sources={independent_source_count}",
        f"high_quality_sources={research_completeness.high_quality_source_count}",
    ]
    if independent_source_count < 2:
        notes.append("limited independent corroboration lowered confidence")
    if low_quality_source_count:
        notes.append(f"low_quality_sources={low_quality_source_count}")
    if unsupported_claim_count:
        notes.append(f"unsupported_claims_excluded={unsupported_claim_count}")
    if contradiction_count:
        notes.append(f"contradictions={contradiction_count}")
    if research_completeness.stop_recommended:
        notes.append("fixture/mock research stop condition met")
    return "; ".join(notes) + "."


def _has_source_quality_type(sources: list[SourceRecord], source_type: str) -> bool:
    return any(
        source.source_quality is not None and source.source_quality.source_type == source_type
        for source in sources
    )


def _missing_information(
    data: dict[str, Any],
    *,
    resolved_url: str | None,
    resolved_linkedin: str | None,
    sources: list[SourceRecord],
    evidence: list[str],
    data_points: list[ResearchDataPoint],
    unsupported_claims: list[str],
) -> list[str]:
    missing = _as_list(data.get("missing_information"))
    source_quality_summary = summarize_source_quality(
        [source.source_quality for source in sources if source.source_quality is not None]
    )
    if not resolved_url:
        missing.append("Company website URL not supplied.")
    if resolved_url and not _has_source_quality_type(sources, "company_site"):
        missing.append("Company website source not available as source-backed evidence.")
    if not resolved_linkedin:
        missing.append("LinkedIn or profile URL not supplied.")
    if not any(
        source.source_type == "linkedin"
        or (source.source_quality is not None and source.source_quality.source_type == "linkedin")
        for source in sources
    ):
        missing.append("LinkedIn or profile-like source not available.")
    if source_quality_summary.independent_source_count < 2:
        missing.append("Independent source corroboration is limited.")
    if not evidence:
        missing.append("No source-backed company claims available.")
    missing_data_points = [data_point for data_point in data_points if not data_point.completed]
    if missing_data_points:
        labels = ", ".join(data_point.label for data_point in missing_data_points[:4])
        missing.append(f"Missing source-backed research data points: {labels}.")
    if not any(_claim_type_for_text(claim) == "company_signal" for claim in evidence):
        missing.append("Recent funding, partnership, validation, or launch signals not confirmed.")
    if unsupported_claims:
        missing.append("Unsupported Keystone experience or outcome claims were excluded.")
    return list(dict.fromkeys(missing))


def _missing_evidence(
    *,
    sources: list[SourceRecord],
    resolved_url: str | None,
    resolved_linkedin: str | None,
    research_completeness: ResearchCompletenessScore,
    data_points: list[ResearchDataPoint],
) -> list[str]:
    missing = source_missing_evidence(
        sources=sources,
        company_url=resolved_url,
        linkedin_url=resolved_linkedin,
    )
    missing.extend(research_completeness.missing_dimensions)
    missing.extend(
        f"{data_point.label}: {data_point.missing_reason}"
        for data_point in data_points
        if not data_point.completed and data_point.missing_reason
    )
    return list(dict.fromkeys(missing))


def _profile_risks(
    data: dict[str, Any],
    *,
    consulting_fit_score: int,
    confidence_score: float,
    data_points: list[ResearchDataPoint],
    unsupported_claims: list[str],
    contradictions: list[str] | None = None,
) -> list[str]:
    risks = _as_list(data.get("risks"))
    if consulting_fit_score < 40:
        risks.append("Low apparent alignment with Keystone target domains.")
    if confidence_score < 0.55:
        risks.append("Low confidence due to limited, low-quality, or uncorroborated sources.")
    if len([data_point for data_point in data_points if data_point.completed]) < 4:
        risks.append("Low research data point coverage; review missing fields before outreach.")
    if unsupported_claims:
        risks.append("Unsupported Keystone experience or outcome claims were flagged and excluded.")
    if contradictions:
        risks.append("Contradictory source-backed claims require human research review.")
    return list(dict.fromkeys(risks))


def research_company_fixture(
    *,
    company_name: str,
    company_url: str | None = None,
    lead_name: str | None = None,
    linkedin_url: str | None = None,
    fixture_json: str | Path | dict[str, Any] | None = None,
    search_results: list[Any] | None = None,
    website_inputs: Any = None,
    profile_inputs: Any = None,
    request_focus_terms: list[str] | None = None,
) -> CompanyProfile:
    """Build a source-attributed company profile without live API calls."""

    data, fixture_ref = _coerce_fixture(fixture_json)
    resolved_name = str(data.get("name") or data.get("company_name") or company_name).strip()
    resolved_url = company_url or data.get("website") or data.get("website_url")
    resolved_linkedin = linkedin_url or data.get("linkedin_url")

    aggregation = aggregate_research_sources(
        data=data,
        company_name=resolved_name,
        company_url=resolved_url,
        linkedin_url=resolved_linkedin,
        fixture_ref=fixture_ref,
        search_results=search_results,
        website_inputs=website_inputs,
        profile_inputs=profile_inputs,
    )
    scored_sources = _score_sources(aggregation.sources, company_url=resolved_url)
    ranked_sources = dedupe_and_rank_source_records(
        scored_sources,
        company_url=resolved_url,
    )
    ranked_sources = _rank_sources_for_request_focus(
        ranked_sources,
        request_focus_terms=request_focus_terms,
    )
    sources, research_completeness = _select_sources_until_complete(
        ranked_sources,
        unsupported_claims=aggregation.unsupported_claims,
    )
    source_quality_summary = summarize_source_quality(_quality_scores_from_sources(sources))
    selected_source_ids = {source.source_id for source in sources}
    raw_claim_records = _claim_records(data, sources)
    excluded_claims = [
        f"claim from source excluded by research stop condition: {claim.claim_text}"
        for claim in raw_claim_records
        if claim.source_id not in selected_source_ids
    ]
    raw_claims = [claim for claim in raw_claim_records if claim.source_id in selected_source_ids]
    source_backed_raw_claims, unsupported_from_source_support = _filter_source_backed_claim_records(
        raw_claims, sources
    )
    claims, unsupported_from_claims = _filter_claim_records(source_backed_raw_claims)
    unsupported_claims = list(
        dict.fromkeys(
            [
                *aggregation.unsupported_claims,
                *excluded_claims,
                *unsupported_from_source_support,
                *unsupported_from_claims,
            ]
        )
    )
    evidence = list(dict.fromkeys(claim.claim_text for claim in claims))
    research_completeness = assess_research_completeness(
        source_scores=_quality_scores_from_sources(sources),
        evidence=evidence,
        unsupported_claims=unsupported_claims,
    )
    contradictions = detect_source_contradictions(sources)
    explicit_features = _coerce_company_features(data)
    inferred_features = _inferred_company_features(sources)
    company_features = _merge_company_features(
        explicit_features,
        inferred_features,
        sources,
    )
    source_backed_features = _source_backed_company_features(company_features, sources)
    combined_text = " ".join(
        [
            resolved_name,
            str(resolved_url or ""),
            str(data.get("description") or ""),
            str(data.get("fit") or ""),
            " ".join(evidence),
            _company_feature_text(source_backed_features),
        ]
    ).lower()
    scores = _score_profile(combined_text)
    description = _best_claim_for_terms(
        claims=evidence,
        terms=("company", "builds", "provides", "platform", "software", "service"),
    ) or (evidence[0] if evidence else "")
    description = description.strip()
    fit_summary = _best_claim_for_terms(
        claims=evidence,
        terms=(
            "fit",
            "relevant",
            "clinical trial",
            "evidence generation",
            "clinical ai",
            "behavioral health",
        ),
    ).strip()
    if not fit_summary:
        fit_summary = (
            "Potential Keystone fit based on source-backed domain alignment."
            if scores["consulting_fit_score"] >= 60
            else "Limited Keystone fit based on available source evidence."
        )
    explicit_data_points = _coerce_research_data_points(
        data,
        sources=sources,
        source_quality_summary_score=source_quality_summary.overall_score,
    )
    inferred_data_points = _inferred_research_data_points(
        data=data,
        sources=sources,
        evidence=evidence,
        scores=scores,
        fit_summary=fit_summary,
        source_quality_summary_score=source_quality_summary.overall_score,
    )
    research_data_points = _merge_research_data_points(
        explicit_data_points,
        inferred_data_points,
    )

    confidence_score = _confidence(
        sources,
        len(evidence),
        data_points=research_data_points,
        features=source_backed_features,
        source_quality_score=source_quality_summary.overall_score,
        research_completeness_score=research_completeness.score,
        independent_source_count=source_quality_summary.independent_source_count,
        low_quality_source_count=source_quality_summary.low_quality_source_count,
        has_website=_has_source_quality_type(sources, "company_site"),
        has_linkedin=any(
            source.source_type == "linkedin"
            or (
                source.source_quality is not None
                and source.source_quality.source_type == "linkedin"
            )
            for source in sources
        ),
        unsupported_claim_count=len(unsupported_claims),
        contradiction_count=len(contradictions),
    )
    confidence_explanation = _confidence_explanation(
        confidence_score=confidence_score,
        source_quality_score=source_quality_summary.overall_score,
        research_completeness=research_completeness,
        independent_source_count=source_quality_summary.independent_source_count,
        low_quality_source_count=source_quality_summary.low_quality_source_count,
        unsupported_claim_count=len(unsupported_claims),
        contradiction_count=len(contradictions),
    )
    missing_evidence = _missing_evidence(
        sources=sources,
        resolved_url=resolved_url,
        resolved_linkedin=resolved_linkedin,
        research_completeness=research_completeness,
        data_points=research_data_points,
    )
    missing = _missing_information(
        data,
        resolved_url=resolved_url,
        resolved_linkedin=resolved_linkedin,
        sources=sources,
        evidence=evidence,
        data_points=research_data_points,
        unsupported_claims=unsupported_claims,
    )
    missing = list(dict.fromkeys([*missing, *missing_evidence]))
    risks = _profile_risks(
        data,
        consulting_fit_score=scores["consulting_fit_score"],
        confidence_score=confidence_score,
        data_points=research_data_points,
        unsupported_claims=unsupported_claims,
        contradictions=contradictions,
    )

    return CompanyProfile(
        name=resolved_name,
        website=resolved_url,
        lead_name=lead_name or data.get("lead_name"),
        linkedin_url=resolved_linkedin,
        description=description,
        fit_summary=fit_summary,
        **scores,
        confidence_score=confidence_score,
        confidence_explanation=confidence_explanation,
        sources=sources,
        source_quality_summary=source_quality_summary,
        research_completeness=research_completeness,
        features=company_features,
        research_data_points=research_data_points,
        evidence=evidence,
        claims=claims,
        unsupported_claims_flagged=unsupported_claims,
        contradictions=contradictions,
        missing_evidence=missing_evidence,
        risks=list(dict.fromkeys(risks)),
        missing_information=list(dict.fromkeys(missing)),
    )


def synthesize_company_profile_from_source_bundle(
    *,
    company_name: str,
    source_bundle: SourceBundle | dict[str, Any],
    company_url: str | None = None,
    lead_name: str | None = None,
    linkedin_url: str | None = None,
) -> CompanyProfile:
    """Build a profile from an LLM-ready source bundle without adding unbacked facts."""

    bundle = (
        source_bundle
        if isinstance(source_bundle, SourceBundle)
        else SourceBundle.model_validate(source_bundle)
    )
    resolved_company_url = company_url or bundle.company_url
    profile = research_company_fixture(
        company_name=company_name or bundle.company_name,
        company_url=resolved_company_url,
        lead_name=lead_name,
        linkedin_url=linkedin_url,
        fixture_json={
            "name": company_name or bundle.company_name,
            "website": resolved_company_url,
            "linkedin_url": linkedin_url,
            "sources": [source.model_dump(mode="json") for source in bundle.sources],
            "missing_information": bundle.missing_evidence,
            "risks": bundle.contradictions,
        },
    )
    payload = profile.model_dump(mode="python")
    payload["contradictions"] = list(
        dict.fromkeys([*profile.contradictions, *bundle.contradictions])
    )
    payload["missing_evidence"] = list(
        dict.fromkeys([*profile.missing_evidence, *bundle.missing_evidence])
    )
    payload["missing_information"] = list(
        dict.fromkeys([*profile.missing_information, *payload["missing_evidence"]])
    )
    return CompanyProfile.model_validate(payload)


def build_llm_ready_source_bundle(
    *,
    company_name: str,
    sources: list[SourceRecord],
    company_url: str | None = None,
    max_sources: int | None = None,
) -> SourceBundle:
    """Create a compact source bundle for model synthesis from source records only."""

    return build_source_bundle(
        company_name=company_name,
        company_url=company_url,
        sources=sources,
        max_sources=max_sources,
    )


def refresh_company_profile_trust(profile: CompanyProfile) -> CompanyProfile:
    """Recompute source quality, completeness, and confidence after local context changes."""

    sources = _score_sources(profile.sources, company_url=profile.website)
    source_quality_summary = summarize_source_quality(_quality_scores_from_sources(sources))
    source_ids = {source.source_id for source in sources}
    claims = [claim for claim in profile.claims if claim.source_id in source_ids]
    unbacked_claims = [
        f"unbacked company claim: {claim.claim_text}"
        for claim in profile.claims
        if claim.source_id not in source_ids
    ]
    evidence = list(dict.fromkeys(claim.claim_text for claim in claims))
    unsupported_claims = list(
        dict.fromkeys([*profile.unsupported_claims_flagged, *unbacked_claims])
    )
    research_completeness = assess_research_completeness(
        source_scores=_quality_scores_from_sources(sources),
        evidence=evidence,
        unsupported_claims=unsupported_claims,
    )
    source_backed_features = _source_backed_company_features(profile.features, sources)
    contradictions = detect_source_contradictions(sources)
    confidence_score = _confidence(
        sources,
        len(evidence),
        data_points=profile.research_data_points,
        features=source_backed_features,
        source_quality_score=source_quality_summary.overall_score,
        research_completeness_score=research_completeness.score,
        independent_source_count=source_quality_summary.independent_source_count,
        low_quality_source_count=source_quality_summary.low_quality_source_count,
        has_website=_has_source_quality_type(sources, "company_site"),
        has_linkedin=any(
            source.source_type == "linkedin"
            or (
                source.source_quality is not None
                and source.source_quality.source_type == "linkedin"
            )
            for source in sources
        ),
        unsupported_claim_count=len(unsupported_claims),
        contradiction_count=len(contradictions),
    )
    missing_evidence = _missing_evidence(
        sources=sources,
        resolved_url=profile.website,
        resolved_linkedin=profile.linkedin_url,
        research_completeness=research_completeness,
        data_points=profile.research_data_points,
    )
    payload = profile.model_dump(mode="python")
    payload.update(
        {
            "confidence_score": confidence_score,
            "confidence_explanation": _confidence_explanation(
                confidence_score=confidence_score,
                source_quality_score=source_quality_summary.overall_score,
                research_completeness=research_completeness,
                independent_source_count=source_quality_summary.independent_source_count,
                low_quality_source_count=source_quality_summary.low_quality_source_count,
                unsupported_claim_count=len(unsupported_claims),
                contradiction_count=len(contradictions),
            ),
            "sources": sources,
            "source_quality_summary": source_quality_summary,
            "research_completeness": research_completeness,
            "evidence": evidence,
            "claims": claims,
            "unsupported_claims_flagged": unsupported_claims,
            "contradictions": contradictions,
            "missing_evidence": missing_evidence,
            "missing_information": list(
                dict.fromkeys([*profile.missing_information, *missing_evidence])
            ),
        }
    )
    return CompanyProfile.model_validate(payload)


def comparison_criterion_label(key: ComparisonCriterionKey) -> str:
    return COMPARISON_CRITERION_LABELS[key]


def parse_company_research_comparison_criteria(
    criteria: str | list[str] | None,
) -> list[ComparisonCriterionKey]:
    values = []
    if criteria is None:
        values = list(DEFAULT_COMPARISON_CRITERIA)
    elif isinstance(criteria, str):
        values = [item.strip() for item in criteria.split(",") if item.strip()]
    else:
        values = [str(item).strip() for item in criteria if str(item).strip()]
    if not values:
        return list(DEFAULT_COMPARISON_CRITERIA)

    normalized: list[ComparisonCriterionKey] = []
    for value in values:
        key = COMPARISON_CRITERION_ALIASES.get(value.lower().replace("-", " "), "")
        if not key:
            raise ValueError(f"Unsupported comparison criterion: {value}")
        normalized.append(key)  # type: ignore[arg-type]
    return list(dict.fromkeys(normalized))


def _profile_data_point_map(
    profile: CompanyProfile,
) -> dict[ResearchDataPointKey, ResearchDataPoint]:
    return {data_point.key: data_point for data_point in profile.research_data_points}


def _profile_feature_source_ids(
    profile: CompanyProfile,
    feature_names: tuple[CompanyFeatureName, ...],
) -> list[str]:
    return list(
        dict.fromkeys(
            feature.source_id
            for feature in profile.source_backed_features
            if feature.feature_name in feature_names
        )
    )


def _criterion_assessment(
    profile: CompanyProfile,
    criterion: ComparisonCriterionKey,
) -> tuple[float, str, list[str], list[str]]:
    data_points = _profile_data_point_map(profile)
    unknowns: list[str] = []
    source_ids: list[str] = []

    if criterion == "consulting_fit":
        data_point = data_points.get("consulting_fit")
        score = float(profile.consulting_fit_score)
        summary = f"Consulting fit {profile.consulting_fit_score}/100."
        if profile.fit_summary:
            summary = f"{summary} {profile.fit_summary}"
        if data_point is not None and data_point.completed:
            source_ids.extend(data_point.source_ids)
        elif data_point is not None and data_point.missing_reason:
            unknowns.append(data_point.missing_reason)
        source_ids.extend(
            _profile_feature_source_ids(
                profile,
                (
                    "clinical_research_relevance",
                    "buyer_function",
                    "implementation_complexity",
                ),
            )
        )
    elif criterion == "evidence_strength":
        source_quality = (
            profile.source_quality_summary.overall_score if profile.source_quality_summary else 0
        )
        completeness = profile.research_completeness.score if profile.research_completeness else 0
        score = (
            (profile.confidence_score * 100.0 * 0.5)
            + (float(source_quality) * 0.25)
            + (float(completeness) * 0.25)
        )
        summary = (
            f"Confidence {profile.confidence_score:.2f}; "
            f"source quality {source_quality}/100; "
            f"research completeness {completeness}/100."
        )
        source_ids.extend(source.source_id for source in profile.sources[:5])
        unknowns.extend(profile.contradictions[:1])
        unknowns.extend(profile.missing_evidence[:2])
    elif criterion == "evidence_generation_need":
        data_point = data_points.get("research_signals")
        score = float(profile.evidence_generation_need)
        summary = f"Evidence generation need {profile.evidence_generation_need}/100."
        if data_point is not None and data_point.completed:
            if data_point.value:
                summary = f"{summary} {data_point.value}"
            source_ids.extend(data_point.source_ids)
        elif data_point is not None and data_point.missing_reason:
            unknowns.append(data_point.missing_reason)
        source_ids.extend(
            _profile_feature_source_ids(
                profile,
                ("clinical_research_relevance", "partnership_signal"),
            )
        )
    elif criterion == "outside_consulting_likelihood":
        data_point = data_points.get("consulting_fit")
        score = float(profile.outside_consulting_likelihood)
        summary = f"Outside consulting likelihood {profile.outside_consulting_likelihood}/100."
        if data_point is not None and data_point.completed:
            source_ids.extend(data_point.source_ids)
        elif data_point is not None and data_point.missing_reason:
            unknowns.append(data_point.missing_reason)
        source_ids.extend(
            _profile_feature_source_ids(
                profile,
                (
                    "implementation_complexity",
                    "compliance_sensitivity",
                    "buyer_function",
                ),
            )
        )
    else:
        behavioral = float(profile.behavioral_health_relevance)
        clinical_ai = float(profile.clinical_ai_relevance)
        cns = float(profile.cns_neuro_relevance)
        score = (behavioral + clinical_ai + cns) / 3.0
        summary = (
            "Clinical relevance composite "
            f"{round(score):.0f}/100 "
            f"(behavioral health {profile.behavioral_health_relevance}, "
            f"clinical AI {profile.clinical_ai_relevance}, "
            f"CNS/neuro {profile.cns_neuro_relevance})."
        )
        for key in ("behavioral_health_relevance", "ai_data_science_relevance"):
            data_point = data_points.get(key)
            if data_point is not None and data_point.completed:
                source_ids.extend(data_point.source_ids)
            elif data_point is not None and data_point.missing_reason:
                unknowns.append(data_point.missing_reason)
        source_ids.extend(
            _profile_feature_source_ids(
                profile,
                ("market_segment", "clinical_research_relevance", "ai_maturity"),
            )
        )

    unique_source_ids = list(dict.fromkeys(source_ids))
    unique_unknowns = list(dict.fromkeys(item for item in unknowns if item))
    adjusted_score = max(0.0, score - (len(unique_unknowns) * 5.0))
    return adjusted_score, summary.strip(), unique_source_ids, unique_unknowns


def compare_company_profiles(
    company_a: CompanyProfile,
    company_b: CompanyProfile,
    *,
    decision_goal: str | None = None,
    criteria: str | list[str] | None = None,
    requested_output_format: str | None = None,
) -> CompanyResearchComparison:
    criteria_keys = parse_company_research_comparison_criteria(criteria)
    entries: list[CompanyResearchComparisonEntry] = []
    company_a_total = 0.0
    company_b_total = 0.0

    for criterion in criteria_keys:
        (
            company_a_score,
            company_a_summary,
            company_a_source_ids,
            company_a_unknowns,
        ) = _criterion_assessment(company_a, criterion)
        (
            company_b_score,
            company_b_summary,
            company_b_source_ids,
            company_b_unknowns,
        ) = _criterion_assessment(company_b, criterion)
        better_fit: ComparisonOutcome
        if not company_a_source_ids and not company_b_source_ids:
            better_fit = "unclear"
        elif abs(company_a_score - company_b_score) < 7:
            better_fit = "tie"
        elif company_a_score > company_b_score:
            better_fit = "company_a"
        else:
            better_fit = "company_b"
        company_a_total += company_a_score
        company_b_total += company_b_score
        rationale = (
            f"{company_a.name}: {company_a_score:.1f} vs {company_b.name}: {company_b_score:.1f}."
        )
        if better_fit == "company_a":
            rationale = (
                f"{rationale} {company_a.name} is stronger on this criterion with fewer "
                "or smaller evidence gaps."
            )
        elif better_fit == "company_b":
            rationale = (
                f"{rationale} {company_b.name} is stronger on this criterion with fewer "
                "or smaller evidence gaps."
            )
        elif better_fit == "tie":
            rationale = (
                f"{rationale} The current evidence is close enough that neither company "
                "clearly leads."
            )
        else:
            rationale = f"{rationale} The available evidence is too thin to call this criterion."
        entries.append(
            CompanyResearchComparisonEntry(
                criterion_key=criterion,
                criterion_label=comparison_criterion_label(criterion),
                company_a_summary=company_a_summary,
                company_a_source_ids=company_a_source_ids,
                company_a_unknowns=company_a_unknowns,
                company_b_summary=company_b_summary,
                company_b_source_ids=company_b_source_ids,
                company_b_unknowns=company_b_unknowns,
                better_fit=better_fit,
                rationale=rationale,
            )
        )

    if abs(company_a_total - company_b_total) < 10:
        recommended_company: ComparisonOutcome = "unclear"
    elif company_a_total > company_b_total:
        recommended_company = "company_a"
    else:
        recommended_company = "company_b"

    winning_criteria = [
        entry.criterion_label for entry in entries if entry.better_fit == recommended_company
    ]
    if recommended_company == "company_a":
        recommendation = (
            f"{company_a.name} is the stronger current Keystone candidate based on "
            f"{', '.join(winning_criteria) or 'the weighted criteria set'}."
        )
    elif recommended_company == "company_b":
        recommendation = (
            f"{company_b.name} is the stronger current Keystone candidate based on "
            f"{', '.join(winning_criteria) or 'the weighted criteria set'}."
        )
    else:
        recommendation = (
            "The comparison is still too close or under-evidenced to recommend one "
            "company over the other."
        )

    evidence_gaps = list(
        dict.fromkeys(
            [f"{company_a.name}: {item}" for item in company_a.missing_evidence]
            + [f"{company_a.name}: {item}" for item in company_a.missing_information]
            + [f"{company_b.name}: {item}" for item in company_b.missing_evidence]
            + [f"{company_b.name}: {item}" for item in company_b.missing_information]
            + [
                f"{company_a.name} {entry.criterion_label}: {item}"
                for entry in entries
                for item in entry.company_a_unknowns
            ]
            + [
                f"{company_b.name} {entry.criterion_label}: {item}"
                for entry in entries
                for item in entry.company_b_unknowns
            ]
        )
    )

    if recommended_company == "company_a":
        next_step = (
            f"Prioritize deeper validation or outreach planning for {company_a.name}, "
            f"then resolve the main gaps still open for {company_b.name} only if needed."
        )
    elif recommended_company == "company_b":
        next_step = (
            f"Prioritize deeper validation or outreach planning for {company_b.name}, "
            f"then resolve the main gaps still open for {company_a.name} only if needed."
        )
    else:
        next_step = (
            "Resolve the highest-impact evidence gaps before choosing which company "
            "should move forward."
        )

    return CompanyResearchComparison(
        company_a=company_a,
        company_b=company_b,
        decision_goal=decision_goal
        or "Assess both companies for possible partnership or advisory relevance to Keystone.",
        decision_criteria=criteria_keys,
        side_by_side_entries=entries,
        recommendation=recommendation,
        recommended_company=recommended_company,
        evidence_gaps=evidence_gaps,
        next_step=next_step,
        requested_output_format=requested_output_format,
    )


def _parse_requested_output_sections(output_format: str | None) -> list[tuple[str, str]]:
    if not output_format:
        return []
    ordered_sections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_item in output_format.split(","):
        label = raw_item.strip()
        if not label:
            continue
        normalized = OUTPUT_SECTION_ALIASES.get(label.lower().replace("-", " "))
        if normalized is None:
            raise ValueError(f"Unsupported output section: {label}")
        if normalized in seen:
            continue
        ordered_sections.append((normalized, label))
        seen.add(normalized)
    return ordered_sections


def _profile_next_step(profile: CompanyProfile) -> str:
    if profile.missing_evidence or profile.confidence_score < 0.7:
        return (
            "Validate the missing evidence with a trusted company source or current news "
            "before treating this as a high-confidence Keystone target."
        )
    return (
        "Use the cited evidence to decide whether to advance to deeper business research "
        "or approved outreach drafting."
    )


def _section_block(
    label: str,
    lines: list[str],
    *,
    strict_format: bool,
    section_key: str,
) -> list[str]:
    heading = label if strict_format else f"## {OUTPUT_SECTION_TITLES[section_key]}"
    block = [heading]
    block.extend(lines or ["- None."])
    return block


def _company_profile_requested_sections(
    profile: CompanyProfile,
    output_format: str,
    *,
    strict_format: bool,
) -> str:
    sections = _parse_requested_output_sections(output_format)
    content = {
        "summary": [
            profile.description or "No source-backed description available.",
            f"Fit: {profile.fit_summary or 'No source-backed fit summary available.'}",
            f"Confidence: {profile.confidence_score:.2f}.",
        ],
        "evidence": [
            f"- {claim.claim_text} [sources: {claim.source_id}]" for claim in profile.claims
        ]
        or ["- No source-backed evidence available."],
        "concerns": [
            f"- {item}"
            for item in dict.fromkeys(
                [
                    *profile.contradictions,
                    *profile.missing_evidence,
                    *profile.missing_information,
                    *profile.risks,
                    *profile.unsupported_claims_flagged,
                ]
            )
        ]
        or ["- No major source-backed concerns flagged."],
        "next_step": [_profile_next_step(profile)],
        "sources": [
            f"- {source.source_id}: {source.title} - {source.url}" for source in profile.sources
        ]
        or ["- No sources available."],
        "unknowns": [f"- {item}" for item in profile.missing_information]
        or ["- No explicit unknowns recorded."],
        "criteria": ["- Not applicable for a single-company profile."],
        "recommendation": ["- Not applicable for a single-company profile."],
    }
    lines: list[str] = []
    for section_key, raw_label in sections:
        if lines:
            lines.append("")
        lines.extend(
            _section_block(
                raw_label,
                content[section_key],
                strict_format=strict_format,
                section_key=section_key,
            )
        )
    return "\n".join(lines)


def _company_research_comparison_requested_sections(
    comparison: CompanyResearchComparison,
    output_format: str,
    *,
    strict_format: bool,
) -> str:
    sections = _parse_requested_output_sections(output_format)
    content = {
        "summary": [
            comparison.decision_goal,
            comparison.recommendation,
        ],
        "evidence": [
            (
                f"- {entry.criterion_label}: "
                f"{comparison.company_a.name}: {entry.company_a_summary} "
                f"[sources: {', '.join(entry.company_a_source_ids) or 'none'}]; "
                f"{comparison.company_b.name}: {entry.company_b_summary} "
                f"[sources: {', '.join(entry.company_b_source_ids) or 'none'}]; "
                f"better fit: {entry.better_fit}; rationale: {entry.rationale}"
            )
            for entry in comparison.side_by_side_entries
        ]
        or ["- No side-by-side evidence available."],
        "concerns": [f"- {item}" for item in comparison.evidence_gaps]
        or ["- No explicit evidence gaps recorded."],
        "next_step": [comparison.next_step],
        "criteria": [
            f"- {comparison_criterion_label(criterion)}"
            for criterion in comparison.decision_criteria
        ],
        "recommendation": [comparison.recommendation],
        "sources": [
            f"- {comparison.company_a.name}: {source.source_id} - {source.title} - {source.url}"
            for source in comparison.company_a.sources
        ]
        + [
            f"- {comparison.company_b.name}: {source.source_id} - {source.title} - {source.url}"
            for source in comparison.company_b.sources
        ],
        "unknowns": [f"- {item}" for item in comparison.evidence_gaps]
        or ["- No explicit unknowns recorded."],
    }
    lines: list[str] = []
    for section_key, raw_label in sections:
        if lines:
            lines.append("")
        lines.extend(
            _section_block(
                raw_label,
                content[section_key],
                strict_format=strict_format,
                section_key=section_key,
            )
        )
    return "\n".join(lines)


def company_research_comparison_markdown(
    comparison: CompanyResearchComparison,
    *,
    output_format: str | None = None,
    strict_format: bool = False,
) -> str:
    requested_output_format = output_format or comparison.requested_output_format
    if requested_output_format:
        return _company_research_comparison_requested_sections(
            comparison,
            requested_output_format,
            strict_format=strict_format,
        )

    lines = [
        f"# {comparison.company_a.name} vs {comparison.company_b.name}",
        "",
        "## Decision Goal",
        comparison.decision_goal,
        "",
        "## Recommendation",
        comparison.recommendation,
        "",
        "## Criteria",
    ]
    for entry in comparison.side_by_side_entries:
        lines.extend(
            [
                f"- {entry.criterion_label}",
                f"  - {comparison.company_a.name}: {entry.company_a_summary}",
                f"    Sources: {', '.join(entry.company_a_source_ids) or 'none'}",
                f"  - {comparison.company_b.name}: {entry.company_b_summary}",
                f"    Sources: {', '.join(entry.company_b_source_ids) or 'none'}",
                f"  - Better fit: {entry.better_fit}",
                f"  - Rationale: {entry.rationale}",
            ]
        )
    lines.extend(["", "## Evidence Gaps"])
    if comparison.evidence_gaps:
        lines.extend(f"- {item}" for item in comparison.evidence_gaps)
    else:
        lines.append("- No explicit evidence gaps recorded.")
    lines.extend(["", "## Next Step", comparison.next_step, "", "## Sources"])
    lines.extend(
        f"- {comparison.company_a.name}: {source.source_id} - {source.title} - {source.url}"
        for source in comparison.company_a.sources
    )
    lines.extend(
        f"- {comparison.company_b.name}: {source.source_id} - {source.title} - {source.url}"
        for source in comparison.company_b.sources
    )
    return "\n".join(lines)


def company_profile_markdown(
    profile: CompanyProfile,
    *,
    output_format: str | None = None,
    strict_format: bool = False,
) -> str:
    """Render a compact human-reviewable markdown profile."""

    if output_format:
        return _company_profile_requested_sections(
            profile,
            output_format,
            strict_format=strict_format,
        )

    source_confidence = (
        profile.source_quality_summary.overall_score if profile.source_quality_summary else 0
    )
    completeness_score = profile.research_completeness.score if profile.research_completeness else 0
    lines = [
        f"# {profile.name}",
        "",
        f"Website: {profile.website or 'Not supplied'}",
        f"LinkedIn/Profile: {profile.linkedin_url or 'Not supplied'}",
        "",
        "## Summary",
        profile.description or "No source-backed description available.",
        "",
        "## Fit",
        profile.fit_summary,
        "",
        "## Scores",
        f"- Behavioral health relevance: {profile.behavioral_health_relevance}",
        f"- Clinical AI relevance: {profile.clinical_ai_relevance}",
        f"- CNS/neuro relevance: {profile.cns_neuro_relevance}",
        f"- Evidence generation need: {profile.evidence_generation_need}",
        f"- Outside consulting likelihood: {profile.outside_consulting_likelihood}",
        f"- Consulting fit score: {profile.consulting_fit_score}",
        f"- Confidence score: {profile.confidence_score:.2f}",
        f"- Source confidence: {source_confidence}/100",
        f"- Research completeness: {completeness_score}/100",
        f"- Confidence explanation: {profile.confidence_explanation}",
        "",
        "## Company Features",
    ]
    source_backed_feature_keys = {
        (feature.feature_name, feature.source_id, feature.evidence_text)
        for feature in profile.source_backed_features
    }
    if profile.features:
        for feature in profile.features:
            support = (
                "source-backed"
                if (feature.feature_name, feature.source_id, feature.evidence_text)
                in source_backed_feature_keys
                else "unbacked"
            )
            lines.append(
                f"- {company_feature_label(feature.feature_name)}: {feature.value} "
                f"({support}; source: {feature.source_id}; "
                f"confidence {feature.confidence:.2f})"
            )
    else:
        lines.append("- No source-backed company features captured.")
    lines.extend(
        [
            "",
            "## Research Data Points",
        ]
    )
    for data_point in profile.research_data_points:
        if data_point.completed:
            sources = ", ".join(data_point.source_ids)
            lines.append(
                f"- {data_point.label}: {data_point.value} "
                f"(confidence {data_point.confidence:.2f}; sources: {sources})"
            )
        else:
            lines.append(f"- {data_point.label}: Missing - {data_point.missing_reason}")
    if profile.contradictions:
        lines.extend(["", "## Contradictions"])
        lines.extend(f"- {item}" for item in profile.contradictions)
    if profile.missing_evidence:
        lines.extend(["", "## Missing Evidence"])
        lines.extend(f"- {item}" for item in profile.missing_evidence)
    lines.extend(
        [
            "",
            "## Sources",
        ]
    )
    for source in profile.sources:
        quality = (
            f", quality {source.source_quality.overall_score}/100"
            if source.source_quality is not None
            else ""
        )
        lines.append(
            f"- {source.title} ({source.source_type}, confidence {source.confidence:.2f}"
            f"{quality}): {source.url}"
        )
    if profile.missing_information:
        lines.extend(["", "## Missing Information"])
        lines.extend(f"- {item}" for item in profile.missing_information)
    return "\n".join(lines)
