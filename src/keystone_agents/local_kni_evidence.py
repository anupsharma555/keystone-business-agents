"""Bounded evidence packet helpers for local KNI document context."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from keystone_agents.tools.kni_document_tool import (
    read_kni_document_file_impl,
    search_kni_documents_impl,
)


def looks_like_local_kni_evidence_lookup(text: str) -> bool:
    lowered = str(text or "").lower()
    has_kni_scope = any(
        marker in lowered
        for marker in (
            "local kni document",
            "local kni documents",
            "kni document",
            "kni documents",
            "keystone neuroinformatics",
            "cfc insurance",
            "cfc policy",
            "proassurance",
            "iao inc",
            "iao, inc",
        )
    )
    has_lookup_shape = any(
        marker in lowered
        for marker in (
            "formation",
            "formed",
            "date filed",
            "certificate of organization",
            "articles of organization",
            "organizer",
            "organized",
            "organised",
            "registered agent",
            "registered office",
            "signer",
            "who filed",
            "filer",
            "formally organized",
            "formally organised",
            "evidence path",
            "evidence",
            "using local",
            "insurance",
            "policy",
            "certificate of insurance",
            "coi",
            "peo",
            "provider",
            "carrier",
            "broker",
            "proposal",
            "capability statement",
            "statement of capabilities",
            "service areas",
            "service offerings",
        )
    )
    return has_kni_scope and has_lookup_shape


def local_kni_live_instruction() -> str:
    return (
        "Answer the user's local KNI document question from "
        "local_kni_evidence_packet and/or guarded KNI document tools. "
        "Do not use hosted file_search for this local evidence question. "
        "Treat local_kni_evidence_packet as bounded evidence, not as a "
        "prewritten final answer. Re-rank the candidate documents against the "
        "latest user question, distinguish related roles such as insurer, "
        "coverholder, broker, producer, agency, organizer, signer, registered "
        "agent, owner, and accountable lead, and say when the evidence does not "
        "support the requested role. Include the evidence path, local_only=true, "
        "send_enabled=false, uncertainty, and any human-review requirement in "
        "the structured output."
    )


def _inferred_lookup_kind(query_text: str, diagnostics: dict[str, Any]) -> str:
    diagnostic_kind = str(diagnostics.get("lookup_kind") or "").lower()
    if diagnostic_kind in {"insurance", "formation"}:
        return diagnostic_kind
    lowered = str(query_text or "").lower()
    if any(
        marker in lowered
        for marker in (
            "insurance",
            "policy",
            "certificate of insurance",
            "coi",
            "peo",
            "provider",
            "carrier",
            "broker",
            "underwriter",
            "producer",
            "agency",
        )
    ):
        return "insurance"
    if any(
        marker in lowered
        for marker in (
            "formation",
            "formed",
            "date filed",
            "certificate of organization",
            "articles of organization",
            "organizer",
            "organized",
            "organised",
            "registered agent",
            "registered office",
            "signer",
            "who filed",
            "filer",
            "formally organized",
            "formally organised",
        )
    ):
        return "formation"
    return diagnostic_kind or "generic"


def _inferred_answer_focus(query_text: str, diagnostics: dict[str, Any]) -> str:
    diagnostic_focus = str(diagnostics.get("answer_focus") or "").lower()
    if diagnostic_focus:
        return diagnostic_focus
    lowered = str(query_text or "").lower()
    if any(
        marker in lowered
        for marker in (
            "organizer",
            "organized",
            "organised",
            "registered agent",
            "registered office",
            "signer",
            "who filed",
            "filer",
            "formally organized",
            "formally organised",
        )
    ):
        return "filing_role"
    if any(marker in lowered for marker in ("broker", "producer", "agency", "agent")):
        return "broker"
    if any(marker in lowered for marker in ("underwriter", "underwriters", "lloyd")):
        return "underwriter"
    return ""


def _packet_search_queries(query_text: str, diagnostics: dict[str, Any]) -> list[str]:
    queries = [str(query_text or "").strip()]
    lookup_kind = _inferred_lookup_kind(query_text, diagnostics)
    answer_focus = _inferred_answer_focus(query_text, diagnostics)
    if lookup_kind == "insurance":
        if answer_focus == "broker":
            queries.extend(
                [
                    "Keystone Neuroinformatics COI broker producer agency ProAssurance IAO",
                    "Keystone Neuroinformatics COI certificate liability producer broker",
                ]
            )
        queries.extend(
            [
                "Keystone Neuroinformatics COI certificate liability insurance producer broker",
                "Keystone Neuroinformatics insurance policy CFC ProAssurance",
                "Keystone Neuroinformatics insurance quote CFC underwriters",
            ]
        )
    elif lookup_kind == "formation":
        if answer_focus == "filing_role":
            queries.extend(
                [
                    (
                        "Keystone Neuroinformatics LLC certificate organization "
                        "organizer registered agent signer filing"
                    ),
                    (
                        "Keystone Neuroinformatics LLC Pennsylvania organization "
                        "registered office organizer signer"
                    ),
                ]
            )
        queries.extend(
            [
                "Keystone Neuroinformatics LLC formation date filed certificate organization",
                "Keystone Neuroinformatics entity file date formation document",
            ]
        )
    diagnostics_query = str(diagnostics.get("query") or "").strip()
    if diagnostics_query:
        queries.append(diagnostics_query)
    return [query for query in dict.fromkeys(queries) if query]


def _candidate_path_score(relative_path: str, diagnostics: dict[str, Any]) -> int:
    path = str(relative_path or "").lower()
    lookup_kind = str(diagnostics.get("lookup_kind") or "").lower()
    answer_focus = str(diagnostics.get("answer_focus") or "").lower()
    evidence_path = str(diagnostics.get("evidence_path") or "").lower()
    score = 0
    if lookup_kind == "insurance":
        if "00_admin/insurance" in path:
            score += 60
        if "coi" in path or "certificate" in path:
            score += 40
        if "insurancepolicy" in path or "insurance/policy" in path:
            score += 30
        if "cfc" in path:
            score += 25
        if "proassurance" in path or "iao" in path:
            score += 25
        if "insurancequote" in path or "insurance/insurancequote" in path:
            score += 18
        if "receipt" in path or "invoice" in path:
            score += 5
        if answer_focus == "broker" and ("coi" in path or "proassurance" in path or "iao" in path):
            score += 35
        if answer_focus == "broker" and "insurancepolicy" in path and "coi" not in path:
            score -= 15
        if "template_sources_to_adapt" in path or "06_archive" in path:
            score -= 50
    elif lookup_kind == "formation":
        if "00_admin/formation" in path:
            score += 70
        if "formation" in path:
            score += 40
        if "certificate" in path or "organization" in path:
            score += 20
        if "template_sources_to_adapt" in path or "06_archive" in path:
            score -= 50
    else:
        if "00_admin" in path:
            score += 10
    if path == evidence_path and (lookup_kind not in {"insurance", "formation"} or score > 0):
        score += 5 if lookup_kind == "insurance" and answer_focus == "broker" else 100
    return score


def local_kni_evidence_paths(packet: object | None) -> list[str]:
    """Return ordered candidate evidence paths from a bounded local KNI packet."""

    if not isinstance(packet, dict):
        return []
    paths: list[str] = []
    for doc in packet.get("candidate_documents") or []:
        if not isinstance(doc, dict):
            continue
        path = str(doc.get("relative_path") or "").strip()
        if path:
            paths.append(path)
    diagnostics = packet.get("retrieval_diagnostics")
    if isinstance(diagnostics, dict):
        evidence_path = str(diagnostics.get("evidence_path") or "").strip()
        if evidence_path:
            paths.append(evidence_path)
        for path_value in diagnostics.get("evidence_paths") or []:
            path = str(path_value or "").strip()
            if path:
                paths.append(path)
    return list(dict.fromkeys(paths))


def build_local_kni_evidence_packet(
    output: object,
    *,
    query_text: str = "",
    max_candidate_documents: int = 5,
) -> dict[str, Any]:
    sources_payload: list[dict[str, Any]] = []
    candidate_paths: list[str] = []
    for source in getattr(output, "sources", []) or []:
        title = str(getattr(source, "title", "") or "")
        if title and "/" in title:
            candidate_paths.append(title)
        sources_payload.append(
            {
                "title": title,
                "url": str(getattr(source, "url", "") or ""),
                "source_type": str(getattr(source, "source_type", "") or ""),
                "note": str(getattr(source, "note", "") or ""),
            }
        )
    diagnostics = getattr(output, "retrieval_diagnostics", {}) or {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    effective_lookup_kind = _inferred_lookup_kind(query_text, diagnostics)
    effective_answer_focus = _inferred_answer_focus(query_text, diagnostics)
    scoring_diagnostics = {
        **diagnostics,
        "lookup_kind": effective_lookup_kind,
        "answer_focus": effective_answer_focus,
    }
    evidence_path = str(diagnostics.get("evidence_path") or "")
    if evidence_path:
        candidate_paths.append(evidence_path)

    search_matches_by_path: dict[str, dict[str, Any]] = {}
    for search_query in _packet_search_queries(query_text, diagnostics):
        try:
            search_payload = search_kni_documents_impl(search_query, max_results=8)
        except Exception:
            continue
        for match in search_payload.get("matches") or []:
            if not isinstance(match, dict):
                continue
            relative_path = str(match.get("relative_path") or "")
            if not relative_path:
                continue
            candidate_paths.append(relative_path)
            search_matches_by_path.setdefault(
                relative_path,
                {
                    "query": search_query,
                    "relative_path": relative_path,
                    "title": match.get("title") or "",
                    "snippet": match.get("snippet") or "",
                    "sensitivity_status": match.get("sensitivity_status") or "",
                    "review_required": bool(match.get("review_required")),
                    "review_reasons": list(match.get("review_reasons") or []),
                    "local_only": True,
                    "send_enabled": False,
                },
            )

    unique_candidate_paths = list(dict.fromkeys(candidate_paths))
    lookup_kind = effective_lookup_kind
    if lookup_kind in {"insurance", "formation"}:
        unique_candidate_paths = [
            path
            for path in unique_candidate_paths
            if _candidate_path_score(path, scoring_diagnostics) > 0
        ]
    unique_candidate_paths.sort(
        key=lambda path: (-_candidate_path_score(path, scoring_diagnostics), path.lower())
    )

    candidate_documents: list[dict[str, Any]] = []
    for path in unique_candidate_paths[:max_candidate_documents]:
        try:
            read_payload = read_kni_document_file_impl(path, max_chars=4_000)
        except Exception as exc:
            candidate_documents.append(
                {
                    "relative_path": path,
                    "read_error": type(exc).__name__,
                    "content_excerpt": "",
                    "local_only": True,
                    "send_enabled": False,
                }
            )
            continue
        content_excerpt = str(read_payload.get("content") or "").strip()[:4_000]
        candidate_documents.append(
            {
                "relative_path": path,
                "title": read_payload.get("title") or "",
                "extension": read_payload.get("extension") or "",
                "sensitivity_status": read_payload.get("sensitivity_status") or "",
                "review_required": bool(read_payload.get("review_required")),
                "review_reasons": list(read_payload.get("review_reasons") or []),
                "truncated": bool(read_payload.get("truncated")),
                "content_excerpt": content_excerpt,
                "local_only": True,
                "send_enabled": False,
                "model_context_allowed": bool(read_payload.get("model_context_allowed", True)),
            }
        )
    return {
        "local_only": True,
        "send_enabled": False,
        "packet_type": "bounded_local_kni_document_evidence",
        "answer_policy": {
            "deterministic_prefetch_is_not_final_answer": True,
            "model_should_reason_from_candidate_documents": True,
            "external_use_requires_explicit_approval": True,
        },
        "search_matches": [
            search_matches_by_path[path]
            for path in unique_candidate_paths
            if path in search_matches_by_path
        ][:12],
        "candidate_documents": candidate_documents,
        "sources": sources_payload,
        "retrieval_diagnostics": {
            **diagnostics,
            "effective_lookup_kind": effective_lookup_kind,
            "effective_answer_focus": effective_answer_focus,
        },
    }


def build_local_kni_evidence_packet_for_query(
    query_text: str,
    *,
    max_candidate_documents: int = 5,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an evidence-only packet directly from the raw user query."""

    seed = SimpleNamespace(
        sources=[],
        retrieval_diagnostics={
            "local_only": True,
            "send_enabled": False,
            "query": str(query_text or "").strip(),
            "prefetch_source": "raw_user_query",
            **(diagnostics or {}),
        },
    )
    return build_local_kni_evidence_packet(
        seed,
        query_text=query_text,
        max_candidate_documents=max_candidate_documents,
    )
