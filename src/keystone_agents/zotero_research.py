"""Local Zotero collection research-brief helpers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keystone_agents.context_env import context_env_path
from keystone_agents.schemas.research import (
    ResearchArticleSummary,
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.source_specific_enrichment import enrich_source_reference
from keystone_agents.tools.search_provider import build_search_provider
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    extract_website_content,
)

DEFAULT_ZOTERO_IMPORT_CACHE = Path(".cache/zotero-import")
DEFAULT_LOCAL_ZOTERO_IMPORT_REPO = Path(
    os.getenv("KEYSTONE_ZOTERO_IMPORT_REPO_DEFAULT", "../zotero-import")
)


def looks_like_zotero_collection_request(text: str) -> bool:
    """Return whether the request asks for local Zotero collection research."""

    lowered = str(text or "").lower()
    return "zotero" in lowered and "collection" in lowered


def looks_like_zotero_article_request(text: str) -> bool:
    """Return whether the request asks to search Zotero for one item/article."""

    lowered = str(text or "").lower()
    if "zotero" not in lowered or "collection" in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "article",
            "paper",
            "item",
            "doi",
            "pmid",
            "pubmed",
            "trial",
            "study",
            "source",
            "summarize",
            "summary",
            "brief",
        )
    )


def extract_zotero_collection_hint(text: str) -> str:
    """Extract a collection hint such as `LH 01` from natural-language text."""

    cleaned = _normalize_quotes(text)
    quoted = [
        match.group(1) or match.group(2)
        for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', cleaned)
    ]
    for value in quoted:
        if _looks_like_collection_hint(value):
            return _normalize_collection_code(value)
    code_match = re.search(r"\b((?:LH|KNI|NIPE)\s*0?\d{1,2})(?:\b|\s*-)", cleaned, flags=re.I)
    if code_match:
        return _normalize_collection_code(code_match.group(1))
    collection_match = re.search(
        r"\bcollection\s+(?:named\s+|called\s+)?\"?([^\".;\n]+)\"?",
        cleaned,
        flags=re.I,
    )
    if collection_match:
        return _normalize_collection_code(collection_match.group(1).strip())
    return ""


def extract_zotero_article_query(text: str) -> str:
    """Extract a Zotero item search query from natural-language text."""

    cleaned = _strip_direct_research_prefix(_normalize_quotes(text))
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", cleaned, flags=re.I)
    if doi_match:
        return doi_match.group(0).rstrip(".,;)")
    nct_match = re.search(r"\bNCT\d{8}\b", cleaned, flags=re.I)
    if nct_match:
        return nct_match.group(0).upper()
    quoted = [
        match.group(1) or match.group(2)
        for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', cleaned)
    ]
    for value in quoted:
        value = " ".join(value.split()).strip()
        if value and "business research analyst" not in value.lower():
            return value[:240]
    for pattern in (
        r"\bzotero\s+(?:article|paper|item|source|study|trial)\s+(?:on|about|called|titled)?\s*([^.;\n]+)",
        r"\b(?:article|paper|item|source|study|trial)\s+(?:on|about|called|titled)?\s*([^.;\n]+)",
    ):
        match = re.search(pattern, cleaned, flags=re.I)
        if match:
            return _clean_article_query(match.group(1))[:240]
    return _clean_article_query(cleaned)[:240]


def build_zotero_article_research_brief(
    query: str,
    *,
    research_goal: str,
    cache_dir: Path | None = None,
) -> tuple[ResearchBrief, list[str]]:
    """Build a deterministic source-backed brief for the best matching Zotero item."""

    cache_path = cache_dir or _zotero_cache_dir()
    ranked = _rank_zotero_items(_load_items(cache_path), query, research_goal=research_goal)
    if not ranked:
        raise ValueError(f"No Zotero item matched: {query}")
    selected = ranked[0][1]
    related = [item for score, item in ranked[:4] if score >= 4]
    if selected not in related:
        related.insert(0, selected)
    selected_data = _item_data(selected)
    selected_id = f"zotero:item:{selected_data.get('key') or 'selected'}"
    selected_title = _clean_text(selected_data.get("title")) or "Untitled Zotero item"
    selected_url = _source_url(selected_data)
    selected_type = _clean_text(selected_data.get("itemType")) or "zotero_item"
    selected_abstract = _clean_text(selected_data.get("abstractNote"))
    selected_is_trial_registry = (
        "clinicaltrials.gov" in selected_url.lower()
        or re.search(r"\bNCT\d{8}\b", selected_title, flags=re.I) is not None
    )

    sources: list[ResearchSourceCitation] = []
    facts: list[ResearchBriefFact] = []
    for index, item in enumerate(related, start=1):
        data = _item_data(item)
        source_id = f"zotero:item:{data.get('key') or index}"
        title = _clean_text(data.get("title")) or f"Zotero item {index}"
        url = _source_url(data) or f"local://zotero_import_cache/{data.get('key') or index}"
        sources.append(
            ResearchSourceCitation(
                source_id=source_id,
                title=title,
                url=url,
                source_type=f"local_zotero:{_clean_text(data.get('itemType')) or 'zotero_item'}",
            )
        )
        facts.append(
            ResearchBriefFact(
                text=f"The local Zotero search matched an item titled {title}.",
                source_ids=[source_id],
                confidence=0.95 if source_id == selected_id else 0.75,
            )
        )

    paragraphs = []
    for index, item in enumerate(related, start=1):
        data = _item_data(item)
        paragraphs.append(
            f"{index}. "
            + _source_paragraph(
                title=_clean_text(data.get("title")) or f"Zotero item {index}",
                abstract=_clean_text(data.get("abstractNote")),
                item_type=_clean_text(data.get("itemType")) or "zotero_item",
                doi=_clean_text(data.get("DOI")),
                url=_source_url(data),
            )
        )
    summary = (
        f"The best local Zotero match for `{query}` is `{selected_title}`. "
        "It should be treated as the primary source for this request."
    )
    unknowns = []
    next_steps = []
    if not selected_abstract:
        unknowns.append(
            "The selected Zotero item has no extracted abstract or full page text "
            "in the local cache."
        )
        next_steps.append(
            "Run live/page extraction for the selected source before relying on detailed methods, "
            "eligibility, or external-facing claims."
        )
    if _wants_eligibility_details(research_goal) and not _has_eligibility_text(selected_abstract):
        summary += (
            " Inclusion and exclusion criteria are not available in the local Zotero metadata."
        )
        unknowns.append(
            "Inclusion and exclusion criteria are not available in the local Zotero metadata."
        )
    if selected_is_trial_registry:
        next_steps.append(
            "Use the ClinicalTrials.gov record for design, arms, enrollment, "
            "and eligibility fields."
        )
    methods_or_design = _methods_or_design(selected_abstract, selected_type)
    relevance_to_goal = _relevance_to_goal(selected_title, selected_abstract)
    if selected_is_trial_registry:
        methods_or_design = (
            "ClinicalTrials.gov registry record; extract the registry fields for "
            "design, arms, enrollment, status, and eligibility."
        )
        relevance_to_goal = (
            "Primary trial-registry source for the requested Lindus/Sooma trial details."
        )

    brief = ResearchBrief(
        target_name=selected_title,
        target_type="zotero_article",
        research_goal=research_goal,
        summary=summary,
        key_findings=[
            (
                "The resolver selected a specific Zotero item instead of treating "
                "the request as a collection."
            ),
            "Supporting Zotero hits are included as related sources when available.",
        ],
        article_summaries=[
            ResearchArticleSummary(
                title=selected_title,
                source_ids=[selected_id],
                research_question=_research_question(selected_title, selected_abstract),
                methods_or_design=methods_or_design,
                key_findings=_key_findings(selected_abstract),
                limitations=_limitations(selected_abstract, selected_type),
                relevance_to_goal=relevance_to_goal,
            )
        ],
        facts=facts,
        inferences=[
            (
                "This item is the likely intended source because it best matches the requested "
                "Zotero article/trial search terms."
            ),
        ],
        unknowns=unknowns,
        limitations=["This is a local dry-run synthesis from Zotero metadata and abstracts only."],
        next_steps=list(dict.fromkeys(next_steps)),
        sources=sources,
    )
    return brief, paragraphs


def build_zotero_live_source_context(
    brief: ResearchBrief,
    *,
    live: bool,
    max_sources: int = 3,
    max_chars_per_source: int = 3500,
    include_web_search: bool = True,
    search_provider_builder: Callable[..., Any] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Extract source pages into a compact SDK synthesis context."""

    metadata: dict[str, Any] = {
        "mode": "live_search" if live else "local",
        "website_extraction_count": 0,
        "source_specific_enrichment_count": 0,
        "web_search_count": 0,
    }
    lines = [
        "Local Zotero resolver selected these approved sources. Use the source_id values "
        "exactly when citing facts.",
    ]
    notes: list[str] = []
    for source in brief.sources[:max_sources]:
        lines.extend(
            [
                "",
                f"Source ID: {source.source_id}",
                f"Title: {source.title}",
                f"URL: {source.url}",
                f"Source type: {source.source_type}",
            ]
        )
        enrichment = enrich_source_reference(
            url=source.url,
            title=source.title,
            source_id=source.source_id,
            live=live and _source_api_enrichment_enabled(),
        )
        if enrichment.status in {"success", "metadata_only"} and enrichment.structured_facts:
            metadata["source_specific_enrichment_count"] = (
                int(metadata["source_specific_enrichment_count"]) + 1
            )
            lines.extend(
                [
                    f"Structured enrichment type: {enrichment.source_type}",
                    f"Structured enrichment status: {enrichment.status}",
                    "Structured facts:",
                    *[f"- {fact}" for fact in enrichment.structured_facts[:16]],
                ]
            )
        if not live:
            lines.append("Extracted text: not requested; only local Zotero metadata is available.")
            continue
        try:
            extraction = extract_website_content(
                source.url,
                company_name=brief.target_name,
                live=True,
            )
        except (WebsiteExtractionError, ValueError, RuntimeError) as exc:
            message = f"Page extraction failed for {source.url}: {exc}"
            notes.append(message)
            lines.append(f"Extraction status: failed ({type(exc).__name__})")
            continue
        metadata["website_extraction_count"] = int(metadata["website_extraction_count"]) + 1
        text = _clean_text(extraction.text_or_markdown)
        if len(text) > max_chars_per_source:
            text = text[: max_chars_per_source - 3].rstrip() + "..."
        lines.extend(
            [
                f"Extraction provider: {extraction.provider}",
                f"Extraction status: {extraction.status}",
                "Extracted text:",
                text or "No readable text extracted.",
            ]
        )
        if extraction.claims:
            lines.extend(["Claim candidates:", *[f"- {claim}" for claim in extraction.claims[:8]]])
    if live and include_web_search:
        search_context, search_notes, search_metadata = _zotero_web_search_context(
            brief,
            search_provider_builder=search_provider_builder,
        )
        if search_context:
            lines.extend(["", search_context])
        notes.extend(search_notes)
        metadata.update(search_metadata)
    return "\n".join(lines), notes, metadata


def _source_api_enrichment_enabled() -> bool:
    raw = os.getenv("KEYSTONE_ENABLE_SOURCE_API_ENRICHMENT")
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _zotero_web_search_context(
    brief: ResearchBrief,
    *,
    search_provider_builder: Callable[..., Any] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    builder = search_provider_builder or build_search_provider
    notes: list[str] = []
    metadata: dict[str, Any] = {"web_search_count": 0}
    try:
        provider = builder(live=True)
    except Exception as exc:
        return "", [f"Web search provider unavailable for Zotero research: {exc}"], metadata

    provider_name = (
        _clean_text(getattr(provider, "provider_name", "")) or provider.__class__.__name__
    )
    metadata["search_provider"] = provider_name
    metadata["provider_usage"] = {provider_name: {"requests_attempted": 0, "results_returned": 0}}
    query_results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for query in _zotero_web_search_queries(brief):
        metadata["provider_usage"][provider_name]["requests_attempted"] += 1
        try:
            results = provider.search_web(query, num_results=4)
        except Exception as exc:
            notes.append(f"Web search failed for `{query}`: {type(exc).__name__}: {exc}")
            continue
        for result in results:
            url = _clean_text(getattr(result, "link", "") or getattr(result, "url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            query_results.append(
                {
                    "title": _clean_text(getattr(result, "title", "")) or "Untitled result",
                    "url": url,
                    "snippet": _clean_text(getattr(result, "snippet", "")),
                    "source": _clean_text(getattr(result, "source", "")) or provider_name,
                    "query": query,
                }
            )
            if len(query_results) >= 8:
                break
        if len(query_results) >= 8:
            break
    metadata["web_search_count"] = len(query_results)
    metadata["provider_usage"][provider_name]["results_returned"] = len(query_results)
    if not query_results:
        return "", notes, metadata
    lines = [
        "Supplemental web-search context. These are approved search snippets; cite the "
        "web_search source_id only if the final source list includes the URL.",
    ]
    for index, result in enumerate(query_results, start=1):
        lines.extend(
            [
                "",
                f"Source ID: web_search:{index}",
                f"Title: {result['title']}",
                f"URL: {result['url']}",
                f"Provider: {result['source']}",
                f"Query: {result['query']}",
                f"Snippet: {result['snippet'] or 'No snippet returned.'}",
            ]
        )
    return "\n".join(lines), notes, metadata


def _zotero_web_search_queries(brief: ResearchBrief) -> list[str]:
    text = " ".join([brief.target_name, *(source.title for source in brief.sources)])
    nct_match = re.search(r"\bNCT\d{8}\b", text, flags=re.I)
    nct = nct_match.group(0).upper() if nct_match else ""
    queries = []
    if nct:
        queries.append(f"{nct} Lindus Sooma tDCS major depressive disorder")
    queries.extend(
        [
            f"{brief.target_name} trial details",
            "Lindus Health Sooma Medical pivotal device clinical trial MDD tDCS",
            "REACH-tDCS Lindus Sooma Medical major depressive disorder trial",
        ]
    )
    return list(dict.fromkeys(query for query in queries if query.strip()))[:4]


def build_zotero_collection_research_brief(
    collection_hint: str,
    *,
    research_goal: str,
    cache_dir: Path | None = None,
) -> tuple[ResearchBrief, list[str]]:
    """Build a deterministic source-backed brief from the Zotero import cache."""

    cache_path = cache_dir or _zotero_cache_dir()
    collections = _load_collections(cache_path)
    collection_name, collection_key = _resolve_collection(collections, collection_hint)
    items = _collection_items(_load_items(cache_path), collection_key)
    if not items:
        raise ValueError(f"No Zotero items found for collection: {collection_name}")

    sources: list[ResearchSourceCitation] = []
    article_summaries: list[ResearchArticleSummary] = []
    paragraphs: list[str] = []
    facts: list[ResearchBriefFact] = []
    for index, item in enumerate(items, start=1):
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if not isinstance(data, dict):
            continue
        source_id = f"zotero:item:{data.get('key') or index}"
        title = _clean_text(data.get("title")) or f"Zotero item {index}"
        url = _source_url(data)
        doi = _clean_text(data.get("DOI"))
        abstract = _clean_text(data.get("abstractNote"))
        source_type = _clean_text(data.get("itemType")) or "zotero_item"
        paragraph = _source_paragraph(
            title=title,
            abstract=abstract,
            item_type=source_type,
            doi=doi,
            url=url,
        )
        paragraphs.append(f"{index}. {paragraph}")
        sources.append(
            ResearchSourceCitation(
                source_id=source_id,
                title=title,
                url=url or f"local://zotero_import_cache/{data.get('key') or index}",
                source_type=f"local_zotero:{source_type}",
            )
        )
        article_summaries.append(
            ResearchArticleSummary(
                title=title,
                source_ids=[source_id],
                research_question=_research_question(title, abstract),
                methods_or_design=_methods_or_design(abstract, source_type),
                key_findings=_key_findings(abstract),
                limitations=_limitations(abstract, source_type),
                relevance_to_goal=_relevance_to_goal(title, abstract),
            )
        )
        facts.append(
            ResearchBriefFact(
                text=f"{title} is included in Zotero collection {collection_name}.",
                source_ids=[source_id],
                confidence=0.95,
            )
        )

    collection_themes = _collection_theme_summary(article_summaries)
    summary = (
        f"The Zotero collection {collection_name} contains {len(article_summaries)} source(s). "
        f"Visible local metadata suggests the main themes are {collection_themes}."
    )
    missing_text_count = sum(
        1
        for summary_item in article_summaries
        if "no extracted abstract" in " ".join(summary_item.limitations).lower()
    )
    brief = ResearchBrief(
        target_name=collection_name,
        target_type="zotero_collection",
        research_goal=research_goal,
        summary=summary,
        key_findings=[
            (
                "The collection was resolved from local Zotero cache metadata, not by "
                "mutating the Zotero library."
            ),
            (
                f"The local cache returned {len(article_summaries)} item(s) with titles, "
                "source IDs, and available URLs."
            ),
            (
                f"{missing_text_count} item(s) lack extracted abstracts or full text in the "
                "local cache and need extraction before external-facing evidence use."
            ),
        ],
        article_summaries=article_summaries,
        facts=facts,
        inferences=[
            (
                "Use this collection as internal literature context first; promote claims "
                "to external-facing use only after reviewing the underlying sources."
            ),
        ],
        unknowns=[
            "The local Zotero cache does not include full extracted text for every webpage source.",
        ],
        limitations=[
            "This is a local dry-run synthesis from Zotero metadata and abstracts only.",
        ],
        next_steps=[
            (
                "Run page or full-text extraction for records without abstracts before "
                "using those records as support for external-facing claims."
            ),
        ],
        sources=sources,
    )
    return brief, paragraphs


def _collection_theme_summary(article_summaries: list[ResearchArticleSummary]) -> str:
    text = " ".join(item.title.lower() for item in article_summaries)
    themes: list[str] = []
    theme_markers = (
        ("psychiatry and psychiatric diagnosis", ("psychiat", "dsm", "rdoc", "mental")),
        ("depression and mood disorders", ("depress", "bipolar", "mood")),
        ("biomarkers and neuroimaging", ("biomarker", "imaging", "neuroimaging", "brain")),
        ("digital psychiatry and health technology", ("digital", "technology", "informatics", "ai")),
        ("clinical trials and interventions", ("trial", "intervention", "treatment", "therapy")),
        ("ethics, governance, or regulation", ("ethic", "governance", "regulat", "fda")),
    )
    for label, markers in theme_markers:
        if any(marker in text for marker in markers):
            themes.append(label)
    if themes:
        return ", ".join(themes[:4])
    return "the titles represented in the local Zotero metadata"


def _zotero_cache_dir() -> Path:
    configured_cache = context_env_path("KEYSTONE_ZOTERO_IMPORT_CACHE", "")
    if str(configured_cache) != "." and configured_cache.exists():
        return configured_cache
    cache_dir = context_env_path("KEYSTONE_ZOTERO_IMPORT_CACHE", str(DEFAULT_ZOTERO_IMPORT_CACHE))
    if cache_dir.exists() and cache_dir != Path("."):
        return cache_dir
    collection_cache = context_env_path("ZOTERO_COLLECTION_CACHE", "")
    if collection_cache.name == "zotero_collections.json":
        return collection_cache.parent
    import_repo = context_env_path("KEYSTONE_ZOTERO_IMPORT_REPO", str(DEFAULT_LOCAL_ZOTERO_IMPORT_REPO))
    if import_repo.exists():
        repo_cache = import_repo / ".cache"
        if repo_cache.exists():
            return repo_cache
    default_local_cache = DEFAULT_LOCAL_ZOTERO_IMPORT_REPO / ".cache"
    if default_local_cache.exists():
        return default_local_cache
    return cache_dir


def _load_collections(cache_path: Path) -> dict[str, str]:
    payload = json.loads((cache_path / "zotero_collections.json").read_text(encoding="utf-8"))
    collections = payload.get("collections") if isinstance(payload, dict) else {}
    if not isinstance(collections, dict):
        raise ValueError("zotero_collections.json must contain a collections object")
    return {str(name): str(key) for name, key in collections.items()}


def _load_items(cache_path: Path) -> list[dict[str, Any]]:
    payload = json.loads((cache_path / "zotero_items.json").read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        raise ValueError("zotero_items.json must contain an items list")
    return [item for item in items if isinstance(item, dict)]


def _resolve_collection(collections: dict[str, str], hint: str) -> tuple[str, str]:
    cleaned_hint = _normalize_collection_code(hint)
    alias_match = _resolve_collection_alias(collections, cleaned_hint)
    if alias_match is not None:
        return alias_match
    if _is_generic_kni_collection_hint(cleaned_hint):
        return _default_kni_collection(collections)
    if not cleaned_hint:
        raise ValueError("A Zotero collection hint is required.")
    lowered = cleaned_hint.lower()
    for name, key in collections.items():
        if name.lower() == lowered:
            return name, key
    for name, key in collections.items():
        if name.lower().startswith(lowered):
            return name, key
    for name, key in collections.items():
        if lowered in name.lower():
            return name, key
    code_hint = _collection_code(cleaned_hint)
    if code_hint:
        lowered_code = code_hint.lower()
        for name, key in collections.items():
            if name.lower().startswith(lowered_code):
                return name, key
        for name, key in collections.items():
            if lowered_code in name.lower():
                return name, key
    raise ValueError(f"No Zotero collection matched: {cleaned_hint}")


def _resolve_collection_alias(collections: dict[str, str], hint: str) -> tuple[str, str] | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", hint.lower()).strip()
    if not normalized:
        return None
    alias_markers = (
        ("kni 00 foundational texts reviews", ("kni", "foundational", "texts", "reviews")),
        ("kni 00 foundational texts reviews", ("kni", "foundational", "text", "review")),
        ("kni 00 foundational texts reviews", ("foundational", "texts", "reviews")),
        ("kni 00 foundational texts reviews", ("foundational", "text", "review")),
    )
    target_code = ""
    for code, required_terms in alias_markers:
        if all(term in normalized for term in required_terms):
            target_code = code
            break
    if not target_code:
        return None
    target_terms = target_code.split()
    for name, key in sorted(collections.items()):
        haystack = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if all(term in haystack for term in target_terms):
            return name, key
    return None


def _is_generic_kni_collection_hint(hint: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", hint.lower()).strip()
    return normalized in {
        "kni",
        "kni collection",
        "kni collections",
        "default kni collection",
        "default kni collections",
        "bounded kni collection",
        "bounded kni collections",
        "one kni collection",
        "local kni collection",
        "local kni collections",
    }


def _default_kni_collection(collections: dict[str, str]) -> tuple[str, str]:
    for name in sorted(collections):
        if name.lower().startswith("kni "):
            return name, collections[name]
    for name in sorted(collections):
        if name.lower().startswith("kni"):
            return name, collections[name]
    raise ValueError("No KNI Zotero collection is available in the local cache.")


def _collection_items(items: list[dict[str, Any]], collection_key: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in items:
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        collections = data.get("collections") if isinstance(data, dict) else []
        if isinstance(collections, list) and collection_key in {
            str(value) for value in collections
        }:
            matches.append(item)
    return matches


def _rank_zotero_items(
    items: list[dict[str, Any]],
    query: str,
    *,
    research_goal: str,
) -> list[tuple[float, dict[str, Any]]]:
    tokens = _query_tokens(query)
    if not tokens:
        return []
    wants_trial_registry = _wants_eligibility_details(research_goal) or any(
        token in {"trial", "study", "nct"} for token in tokens
    )
    ranked: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        data = _item_data(item)
        haystack = _item_haystack(data)
        title = _clean_text(data.get("title")).lower()
        score = sum(3.0 if token in title else 1.0 for token in tokens if token in haystack)
        url = _source_url(data).lower()
        item_type = _clean_text(data.get("itemType")).lower()
        if "sooma" in tokens and "sooma" in haystack:
            score += 4
        if "lindus" in tokens and "lindus" in haystack:
            score += 4
        if "reach" in tokens and "reach" in haystack:
            score += 3
        if wants_trial_registry and (
            "clinicaltrials.gov" in url or re.search(r"\bnct\d{8}\b", title, flags=re.I)
        ):
            score += 18
        if any(token in {"article", "paper", "pubmed", "doi"} for token in tokens) and (
            item_type == "journalarticle" or "pubmed" in url
        ):
            score += 2
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda entry: (-entry[0], _clean_text(_item_data(entry[1]).get("title"))))
    return ranked


def _item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    return data if isinstance(data, dict) else {}


def _item_haystack(data: dict[str, Any]) -> str:
    return " ".join(
        _clean_text(data.get(key)).lower()
        for key in ("key", "title", "url", "abstractNote", "DOI", "extra", "itemType")
    )


def _query_tokens(text: str) -> set[str]:
    stop_words = {
        "about",
        "agent",
        "analysit",
        "analyst",
        "and",
        "article",
        "ask",
        "brief",
        "business",
        "design",
        "exclusion",
        "find",
        "include",
        "inclusion",
        "info",
        "method",
        "methods",
        "other",
        "paragraph",
        "paper",
        "relevant",
        "research",
        "summary",
        "summarize",
        "the",
        "trial",
        "with",
        "zotero",
    }
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize_quotes(text).lower())
        if len(token) > 2 and token not in stop_words
    }
    if re.search(r"\btrial\b|\bstudy\b", text, flags=re.I):
        tokens.add("trial")
    nct_match = re.search(r"\bNCT\d{8}\b", text, flags=re.I)
    if nct_match:
        tokens.add(nct_match.group(0).lower())
        tokens.add("nct")
    return tokens


def _wants_eligibility_details(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("inclusion", "exclusion", "eligibility"))


def _has_eligibility_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("inclusion", "exclusion", "eligibility"))


def _strip_direct_research_prefix(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().strip("'\"").split())
    cleaned = re.sub(r"^@?\w+\s+", "", cleaned) if cleaned.lower().startswith("@kni ") else cleaned
    cleaned = re.sub(
        r"^(?:ask\s+the\s+)?business\s+research\s+analys(?:t|it)\s+to\s+",
        "",
        cleaned,
        flags=re.I,
    )
    return cleaned


def _clean_article_query(text: str) -> str:
    cleaned = _strip_direct_research_prefix(text)
    cleaned = re.sub(
        r"\binclude\b.*$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"^(?:find\s+and\s+summarize|summarize|find|search)\s+",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"^(?:the\s+)?zotero\s+(?:article|paper|item|source|study|trial)\s+",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^(?:on|about)\s+", "", cleaned, flags=re.I)
    return " ".join(cleaned.strip(" .;:'\"").split())


def _normalize_quotes(text: str) -> str:
    return (
        str(text or "")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _looks_like_collection_hint(value: str) -> bool:
    lowered = value.lower()
    return bool(re.search(r"\b(?:lh|kni|nipe)\s*0?\d{1,2}\b", lowered)) or "zotero" in lowered


def _normalize_collection_code(value: str) -> str:
    cleaned = " ".join(_normalize_quotes(value).strip().strip("'\"").split())
    match = re.match(r"^(LH|KNI|NIPE)\s+(\d)\b(.*)$", cleaned, flags=re.I)
    if match:
        return f"{match.group(1).upper()} 0{match.group(2)}{match.group(3)}".strip()
    return cleaned


def _collection_code(value: str) -> str:
    match = re.search(r"\b((?:LH|KNI|NIPE)\s*0?\d{1,2})\b", value, flags=re.I)
    return _normalize_collection_code(match.group(1)) if match else ""


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_url(data: dict[str, Any]) -> str:
    url = _clean_text(data.get("url"))
    if url:
        return url
    doi = _clean_text(data.get("DOI"))
    return f"https://doi.org/{doi}" if doi else ""


def _source_paragraph(
    *,
    title: str,
    abstract: str,
    item_type: str,
    doi: str,
    url: str,
) -> str:
    source_tail = ""
    if url:
        source_tail = f" Source: {url}"
    if doi:
        source_tail += f" DOI: {doi}"
    if not abstract:
        return (
            f"`{title}` is a Zotero {item_type} record with no extracted abstract in "
            "the local cache. It is useful as source context, but the page should be "
            "extracted before relying on it for external-facing claims."
            f"{source_tail}"
        )
    return f"`{title}` {_first_sentences(abstract, max_sentences=3)}{source_tail}"


def _first_sentences(text: str, *, max_sentences: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    selected = " ".join(
        sentence.strip() for sentence in sentences[:max_sentences] if sentence.strip()
    )
    if len(selected) > 650:
        selected = selected[:647].rstrip() + "..."
    return selected


def _research_question(title: str, abstract: str) -> str:
    if "acceptability" in title.lower():
        return "How acceptable is home-based tDCS for participants with major depression?"
    if "follow-up" in title.lower() or "6-month" in title.lower():
        return "Do clinical responses persist after home-based tDCS treatment?"
    if "randomized" in title.lower() or "sham-controlled" in title.lower():
        return "Does remote home-based tDCS improve depressive symptoms compared with sham?"
    if abstract:
        return _first_sentences(abstract, max_sentences=1)
    return "Not available from local Zotero metadata."


def _methods_or_design(abstract: str, item_type: str) -> str:
    lowered = abstract.lower()
    if "randomized" in lowered or "randomised" in lowered:
        return "Remote randomized sham-controlled trial context."
    if "open-label" in lowered or "single-arm" in lowered:
        return "Open-label single-arm feasibility context."
    if "mixed methods" in lowered or "thematic analysis" in lowered:
        return "Mixed-methods qualitative analysis context."
    return f"Local Zotero {item_type} metadata."


def _key_findings(abstract: str) -> list[str]:
    if not abstract:
        return []
    findings: list[str] = []
    for marker in (
        "significant",
        "response",
        "acceptability",
        "safety",
        "feasibility",
        "sustained",
    ):
        if marker in abstract.lower():
            findings.append(f"Abstract includes a {marker} signal.")
    return findings[:3]


def _limitations(abstract: str, item_type: str) -> list[str]:
    limitations: list[str] = []
    if not abstract:
        limitations.append("No abstract was extracted into the local Zotero cache.")
    if "open-label" in abstract.lower() or "single-arm" in abstract.lower():
        limitations.append("Open-label or single-arm design limits efficacy inference.")
    if item_type == "webpage":
        limitations.append("Webpage source should be extracted before external use.")
    return limitations


def _relevance_to_goal(title: str, abstract: str) -> str:
    text = f"{title} {abstract}".lower()
    if "lindus" in text or "clinicaltrials.gov" in text:
        return "Supports trial, company, or registry context for REACH-tDCS."
    if "randomized" in text or "sham" in text:
        return "Supports evidence review for home-based tDCS efficacy and trial design."
    if "acceptability" in text:
        return "Supports patient-experience and implementation review."
    return "Supports local source context for the requested Zotero collection."
