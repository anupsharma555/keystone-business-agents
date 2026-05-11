"""Source-specific enrichment for high-value research URLs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import requests
from pydantic import BaseModel, Field


class SourceSpecificEnrichment(BaseModel):
    """Structured facts extracted from a known source family."""

    source_id: str = ""
    title: str = ""
    url: str = ""
    source_type: str = "generic"
    status: str = "skipped"
    structured_facts: list[str] = Field(default_factory=list)
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def enrich_source_reference(
    *,
    url: str,
    title: str = "",
    source_id: str = "",
    live: bool = False,
    http_get: Callable[..., Any] | None = None,
) -> SourceSpecificEnrichment:
    """Return structured enrichment for ClinicalTrials, PubMed, DOI, or Zotero references."""

    source_type = classify_source_reference(url=url, title=title, source_id=source_id)
    base = {
        "source_id": source_id,
        "title": title,
        "url": url,
        "source_type": source_type,
    }
    if source_type == "zotero":
        key = _zotero_key(source_id)
        return SourceSpecificEnrichment(
            **base,
            status="metadata_only",
            structured_facts=[f"Local Zotero item key: {key}"] if key else [],
            metadata={"zotero_key": key} if key else {},
        )
    if not live:
        return SourceSpecificEnrichment(**base, status="dry_run")
    get = http_get or requests.get
    if source_type == "clinical_trials":
        return _enrich_clinical_trials(**base, http_get=get)
    if source_type == "pubmed":
        return _enrich_pubmed(**base, http_get=get)
    if source_type == "doi":
        return _enrich_crossref(**base, http_get=get)
    return SourceSpecificEnrichment(**base, status="skipped")


def classify_source_reference(*, url: str, title: str = "", source_id: str = "") -> str:
    """Classify a source reference by stable URL/title/source-id markers."""

    text = " ".join([url, title, source_id]).lower()
    if "clinicaltrials.gov" in text or re.search(r"\bNCT\d{8}\b", text, flags=re.I):
        return "clinical_trials"
    if "pubmed.ncbi.nlm.nih.gov" in text or re.search(r"\bPMID:?\s*\d{6,9}\b", text, flags=re.I):
        return "pubmed"
    if "doi.org/" in text or re.search(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", text, flags=re.I):
        return "doi"
    if source_id.startswith("zotero:item:"):
        return "zotero"
    return "generic"


def _enrich_clinical_trials(
    *,
    source_id: str,
    title: str,
    url: str,
    source_type: str,
    http_get: Callable[..., Any],
) -> SourceSpecificEnrichment:
    nct_id = _nct_id(" ".join([url, title, source_id]))
    if not nct_id:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="missing_identifier",
        )
    endpoint = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    try:
        response = http_get(endpoint, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="error",
            metadata={"error": f"{type(exc).__name__}: {exc}"},
        )
    protocol = payload.get("protocolSection") if isinstance(payload, dict) else {}
    if not isinstance(protocol, dict):
        protocol = {}
    identification = _mapping(protocol.get("identificationModule"))
    status = _mapping(protocol.get("statusModule"))
    design = _mapping(protocol.get("designModule"))
    eligibility = _mapping(protocol.get("eligibilityModule"))
    contacts = _mapping(protocol.get("contactsLocationsModule"))
    description = _mapping(protocol.get("descriptionModule"))
    facts = [
        _fact("NCT ID", identification.get("nctId") or nct_id),
        _fact("Brief title", identification.get("briefTitle") or title),
        _fact("Official title", identification.get("officialTitle")),
        _fact("Overall status", status.get("overallStatus")),
        _fact("Start date", _date_value(status.get("startDateStruct"))),
        _fact("Completion date", _date_value(status.get("completionDateStruct"))),
        _fact("Enrollment", _enrollment_value(design.get("enrollmentInfo"))),
        _fact("Study type", design.get("studyType")),
        _fact("Phases", ", ".join(design.get("phases") or [])),
        _fact("Design", _design_summary(design)),
        _fact("Eligibility", eligibility.get("eligibilityCriteria")),
        _fact("Sex", eligibility.get("sex")),
        _fact("Minimum age", eligibility.get("minimumAge")),
        _fact("Maximum age", eligibility.get("maximumAge")),
        _fact("Locations", _locations_summary(contacts)),
        _fact("Brief summary", description.get("briefSummary")),
    ]
    facts = [fact for fact in facts if fact]
    return SourceSpecificEnrichment(
        source_id=source_id,
        title=str(identification.get("briefTitle") or title or nct_id),
        url=url,
        source_type=source_type,
        status="success",
        structured_facts=facts,
        text="\n".join(facts),
        metadata={"nct_id": nct_id, "api_url": endpoint},
    )


def _enrich_pubmed(
    *,
    source_id: str,
    title: str,
    url: str,
    source_type: str,
    http_get: Callable[..., Any],
) -> SourceSpecificEnrichment:
    pmid = _pmid(" ".join([url, title, source_id]))
    if not pmid:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="missing_identifier",
        )
    endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    try:
        response = http_get(
            endpoint,
            params={"db": "pubmed", "id": pmid, "retmode": "xml"},
            timeout=20,
        )
        response.raise_for_status()
        root = ET.fromstring(str(response.text or ""))
    except Exception as exc:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="error",
            metadata={"error": f"{type(exc).__name__}: {exc}"},
        )
    article_title = _xml_text(root, ".//ArticleTitle") or title
    abstract = " ".join(_xml_texts(root, ".//AbstractText"))
    journal = _xml_text(root, ".//Journal/Title")
    year = _xml_text(root, ".//PubDate/Year")
    doi = _article_id(root, "doi")
    facts = [
        _fact("PMID", pmid),
        _fact("Title", article_title),
        _fact("Journal", journal),
        _fact("Year", year),
        _fact("DOI", doi),
        _fact("Abstract", abstract),
    ]
    facts = [fact for fact in facts if fact]
    return SourceSpecificEnrichment(
        source_id=source_id,
        title=article_title,
        url=url,
        source_type=source_type,
        status="success",
        structured_facts=facts,
        text="\n".join(facts),
        metadata={"pmid": pmid, "api_url": endpoint},
    )


def _enrich_crossref(
    *,
    source_id: str,
    title: str,
    url: str,
    source_type: str,
    http_get: Callable[..., Any],
) -> SourceSpecificEnrichment:
    doi = _doi(" ".join([url, title, source_id]))
    if not doi:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="missing_identifier",
        )
    endpoint = f"https://api.crossref.org/works/{doi}"
    try:
        response = http_get(endpoint, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return SourceSpecificEnrichment(
            source_id=source_id,
            title=title,
            url=url,
            source_type=source_type,
            status="error",
            metadata={"error": f"{type(exc).__name__}: {exc}"},
        )
    message = _mapping(payload.get("message") if isinstance(payload, dict) else {})
    resolved_title = _first(message.get("title")) or title
    facts = [
        _fact("DOI", doi),
        _fact("Title", resolved_title),
        _fact("Publisher", message.get("publisher")),
        _fact("Type", message.get("type")),
        _fact(
            "Published",
            _date_parts(message.get("published-print") or message.get("published-online")),
        ),
        _fact("Container", _first(message.get("container-title"))),
        _fact("Abstract", message.get("abstract")),
    ]
    facts = [fact for fact in facts if fact]
    return SourceSpecificEnrichment(
        source_id=source_id,
        title=resolved_title,
        url=url,
        source_type=source_type,
        status="success",
        structured_facts=facts,
        text="\n".join(facts),
        metadata={"doi": doi, "api_url": endpoint},
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fact(label: str, value: Any) -> str:
    text = str(value or "").strip()
    return f"{label}: {text}" if text else ""


def _first(value: Any) -> str:
    if isinstance(value, list | tuple) and value:
        return str(value[0] or "").strip()
    return str(value or "").strip()


def _nct_id(text: str) -> str:
    match = re.search(r"\bNCT\d{8}\b", text or "", flags=re.I)
    return match.group(0).upper() if match else ""


def _pmid(text: str) -> str:
    match = re.search(r"(?:PMID:?\s*)?(\d{6,9})(?:/?$|\b)", text or "", flags=re.I)
    if match and ("pubmed" in text.lower() or "pmid" in text.lower()):
        return match.group(1)
    return ""


def _doi(text: str) -> str:
    match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text or "", flags=re.I)
    return match.group(0).rstrip(".,;)").lower() if match else ""


def _zotero_key(source_id: str) -> str:
    prefix = "zotero:item:"
    return source_id.removeprefix(prefix).strip() if source_id.startswith(prefix) else ""


def _date_value(value: Any) -> str:
    mapping = _mapping(value)
    return str(mapping.get("date") or mapping.get("type") or "").strip()


def _enrollment_value(value: Any) -> str:
    mapping = _mapping(value)
    count = mapping.get("count")
    kind = mapping.get("type")
    if count and kind:
        return f"{count} ({kind})"
    return str(count or kind or "").strip()


def _design_summary(design: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("allocation", "interventionModel", "maskingInfo", "primaryPurpose"):
        design_info = design.get("designInfo")
        value = design_info.get(key) if isinstance(design_info, dict) else ""
        if value:
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


def _locations_summary(contacts: dict[str, Any]) -> str:
    locations = contacts.get("locations")
    if not isinstance(locations, list):
        return ""
    names = []
    for location in locations[:5]:
        if not isinstance(location, dict):
            continue
        names.append(
            ", ".join(
                str(location.get(key) or "").strip()
                for key in ("facility", "city", "state", "country")
                if str(location.get(key) or "").strip()
            )
        )
    return "; ".join(name for name in names if name)


def _xml_text(root: ET.Element, path: str) -> str:
    element = root.find(path)
    return "".join(element.itertext()).strip() if element is not None else ""


def _xml_texts(root: ET.Element, path: str) -> list[str]:
    return ["".join(element.itertext()).strip() for element in root.findall(path)]


def _article_id(root: ET.Element, id_type: str) -> str:
    for element in root.findall(".//ArticleId"):
        if element.attrib.get("IdType") == id_type:
            return "".join(element.itertext()).strip()
    return ""


def _date_parts(value: Any) -> str:
    mapping = _mapping(value)
    parts = mapping.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list):
        return "-".join(str(part) for part in parts[0])
    return ""
