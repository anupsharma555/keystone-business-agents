"""Reusable source-triage contract for search-heavy agent runs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from keystone_agents.source_registry import (
    classify_source_lanes,
    required_source_lanes_for_company,
    required_source_lanes_for_opportunity,
    result_mapping,
    result_url,
)

SourceTriageDecision = Literal["retain", "review", "reject", "deepen"]

_FORMAL_REQUEST_RE = re.compile(
    r"\b(?:grants?|nofo|foa|rfa|funding opportunit(?:y|ies)|awards?|rfps?|"
    r"requests?\s+for\s+proposals?|solicitations?|procurement|pilots?|"
    r"partnership opportunities?|call[- ]for[- ]proposals?|cfps?)\b",
    flags=re.I,
)
_DEEP_REQUEST_RE = re.compile(
    r"\b(?:deep|deeper|detailed|source[- ]backed|synthesis|summari[sz]e|"
    r"extract|read|compare|broad|broaden)\b",
    flags=re.I,
)
_SOURCE_CONTRADICTION_RE = re.compile(
    r"\b(?:"
    r"not\s+(?:a\s+)?notice\s+of\s+funding\s+opportunity|"
    r"not\s+(?:an?\s+)?(?:nofo|foa|rfa|rfp|grant|solicitation|procurement)|"
    r"not\s+(?:an?\s+)?(?:active\s+)?(?:funding|grant|award|pilot|partnership)\s+opportunit(?:y|ies)|"
    r"does\s+not\s+(?:accept|invite|request)\s+(?:applications?|proposals?|submissions?)|"
    r"applications?\s+(?:are\s+)?(?:closed|no\s+longer\s+accepted)|"
    r"deadline\s+(?:has\s+)?passed|"
    r"apply\s+through\s+an?\s+appropriate\s+nih\s+parent\s+funding\s+announcement"
    r")\b",
    flags=re.I,
)
_FORMAL_ACTIONABILITY_RE = re.compile(
    r"\b(?:"
    r"apply|applications?|applicants?|proposals?|submissions?|letters?\s+of\s+intent|loi|"
    r"deadline|due\s+date|closing\s+date|posted\s+date|notice\s+of\s+funding|nofo|foa|rfa|"
    r"rfp|rfi|solicitation|procurement|request\s+for\s+proposals?|sources\s+sought|"
    r"grant\s+applications?|funding\s+opportunity|sbir|sttr|eligib(?:le|ility)|"
    r"small\s+business(?:es)?|vendors?|contractors?|subcontractors?|partners?|"
    r"pilot\s+(?:program|partner|vendor|award|funding)|award\s+amount|total\s+funding"
    r")\b",
    flags=re.I,
)
_FORMAL_RECOGNITION_ONLY_RE = re.compile(
    r"\b(?:"
    r"award\s+winners?|winner\s+announced|announces?\s+(?:the\s+)?(?:\d{4}\s+)?award\s+winners?|"
    r"recognizes?|celebrat(?:e|es|ing)|honorees?|rankings?|best\s+(?:of|in)\b"
    r")\b",
    flags=re.I,
)
_STOPWORDS = frozenset(
    {
        "about",
        "active",
        "agent",
        "and",
        "are",
        "around",
        "based",
        "being",
        "broad",
        "brief",
        "beyond",
        "caveats",
        "can",
        "compare",
        "comparison",
        "company",
        "create",
        "current",
        "deep",
        "deeper",
        "detailed",
        "doing",
        "evidence",
        "from",
        "give",
        "include",
        "into",
        "keystone",
        "latest",
        "please",
        "query",
        "read",
        "recent",
        "recently",
        "search",
        "source",
        "source-backed",
        "sources",
        "summarize",
        "synthesis",
        "that",
        "the",
        "this",
        "used",
        "using",
        "what",
        "when",
        "where",
        "who",
        "with",
    }
)


class SourceTriageCandidate(BaseModel):
    """Compact source candidate passed into source triage."""

    source_id: str = ""
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    source_type: str = ""
    published_at: str | None = None
    lanes: list[str] = Field(default_factory=list)
    extraction_status: str = ""
    evidence_excerpt: str = ""
    key_facts: list[str] = Field(default_factory=list)
    supported_claims: list[str] = Field(default_factory=list)


class SourceTriageItem(BaseModel):
    """One source-triage decision."""

    source_id: str
    title: str = ""
    url: str = ""
    decision: SourceTriageDecision
    relevance_score: int = Field(ge=0, le=100)
    directness_score: int = Field(ge=0, le=100)
    lanes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    rationale: str = ""


class SourceTriageResult(BaseModel):
    """Triage summary for retrieval candidates before synthesis."""

    request_text: str = ""
    agent_name: str = ""
    mode: str = "fixture_safe_source_triage"
    expected_lanes: list[str] = Field(default_factory=list)
    decisions: list[SourceTriageItem] = Field(default_factory=list)
    retained_source_ids: list[str] = Field(default_factory=list)
    retained_urls: list[str] = Field(default_factory=list)
    review_source_ids: list[str] = Field(default_factory=list)
    review_urls: list[str] = Field(default_factory=list)
    rejected_source_ids: list[str] = Field(default_factory=list)
    rejected_urls: list[str] = Field(default_factory=list)
    deepen_source_ids: list[str] = Field(default_factory=list)
    deepen_urls: list[str] = Field(default_factory=list)
    recall_gaps: list[str] = Field(default_factory=list)
    needs_broaden_or_deepen: bool = False
    recommended_action: str = ""


class SourceTriageSummaryItem(BaseModel):
    """Bounded source decision promoted into specialist and synthesis context."""

    source_id: str = ""
    title: str = ""
    url: str = ""
    decision: SourceTriageDecision
    relevance_score: int = Field(default=0, ge=0, le=100)
    directness_score: int = Field(default=0, ge=0, le=100)
    rationale: str = ""


class SourceTriageSummary(BaseModel):
    """Compact validated source-selection contract for downstream reasoning."""

    mode: str = ""
    recommended_action: str = ""
    needs_broaden_or_deepen: bool = False
    decision_counts: dict[SourceTriageDecision, int] = Field(default_factory=dict)
    retained_source_ids: list[str] = Field(default_factory=list)
    retained_urls: list[str] = Field(default_factory=list)
    review_source_ids: list[str] = Field(default_factory=list)
    review_urls: list[str] = Field(default_factory=list)
    rejected_source_ids: list[str] = Field(default_factory=list)
    rejected_urls: list[str] = Field(default_factory=list)
    deepen_source_ids: list[str] = Field(default_factory=list)
    deepen_urls: list[str] = Field(default_factory=list)
    recall_gaps: list[str] = Field(default_factory=list)
    decisions: list[SourceTriageSummaryItem] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> SourceTriageSummary:
        """Normalize a full or compact legacy triage payload."""

        if isinstance(payload, cls):
            return payload
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        if not isinstance(payload, dict) or not payload:
            return cls()
        raw_decisions = payload.get("decisions")
        decisions: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        if isinstance(raw_decisions, list):
            for item in raw_decisions[:8]:
                if not isinstance(item, dict):
                    continue
                decision = str(item.get("decision") or "").strip()
                if decision not in {"retain", "review", "reject", "deepen"}:
                    continue
                counts[decision] = counts.get(decision, 0) + 1
                decisions.append(
                    {
                        "source_id": str(item.get("source_id") or "")[:120],
                        "title": str(item.get("title") or "")[:160],
                        "url": str(item.get("url") or "")[:500],
                        "decision": decision,
                        "relevance_score": _bounded_score(item.get("relevance_score")),
                        "directness_score": _bounded_score(item.get("directness_score")),
                        "rationale": str(item.get("rationale") or "")[:260],
                    }
                )
        raw_counts = payload.get("decision_counts")
        if isinstance(raw_counts, dict):
            counts = {
                str(key): max(0, int(value))
                for key, value in raw_counts.items()
                if str(key) in {"retain", "review", "reject", "deepen"}
                and isinstance(value, int | float)
            }
        return cls.model_validate(
            {
                "mode": str(payload.get("mode") or ""),
                "recommended_action": str(payload.get("recommended_action") or ""),
                "needs_broaden_or_deepen": bool(payload.get("needs_broaden_or_deepen")),
                "decision_counts": counts,
                "retained_source_ids": _bounded_strings(payload.get("retained_source_ids"), 8),
                "retained_urls": _bounded_strings(payload.get("retained_urls"), 8),
                "review_source_ids": _bounded_strings(payload.get("review_source_ids"), 8),
                "review_urls": _bounded_strings(payload.get("review_urls"), 8),
                "rejected_source_ids": _bounded_strings(payload.get("rejected_source_ids"), 8),
                "rejected_urls": _bounded_strings(payload.get("rejected_urls"), 8),
                "deepen_source_ids": _bounded_strings(payload.get("deepen_source_ids"), 8),
                "deepen_urls": _bounded_strings(payload.get("deepen_urls"), 8),
                "recall_gaps": _bounded_strings(payload.get("recall_gaps"), 6),
                "decisions": decisions,
            }
        )

    def has_evidence(self) -> bool:
        """Return whether this summary carries any triage decision or boundary."""

        return bool(
            self.mode
            or self.recommended_action
            or self.needs_broaden_or_deepen
            or self.decisions
            or self.retained_source_ids
            or self.review_source_ids
            or self.rejected_source_ids
            or self.deepen_source_ids
            or self.recall_gaps
        )


def _bounded_score(value: Any) -> int:
    try:
        return max(0, min(100, int(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def _bounded_strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()][:limit]


def triage_source_candidates(
    *,
    request_text: str,
    candidates: Sequence[Any],
    agent_name: str = "",
    max_retain: int = 5,
) -> SourceTriageResult:
    """Classify retrieved source candidates before downstream synthesis.

    This is intentionally deterministic and fixture-safe. It provides the same
    structured contract that a live SDK source-reranker can populate later.
    """

    request = " ".join(str(request_text or "").split())
    agent = str(agent_name or "").strip()
    expected_lanes = _expected_lanes(request_text=request, agent_name=agent)
    focus_terms = _focus_terms(request)
    formal_request = bool(_FORMAL_REQUEST_RE.search(request))
    deep_request = bool(_DEEP_REQUEST_RE.search(request))

    decisions: list[SourceTriageItem] = []
    retained_count = 0
    observed_lanes: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        compact = _candidate_from_any(candidate, index=index)
        observed_lanes.update(compact.lanes)
        item = _triage_one_candidate(
            compact,
            request_text=request,
            focus_terms=focus_terms,
            expected_lanes=expected_lanes,
            formal_request=formal_request,
            deep_request=deep_request,
            retained_count=retained_count,
            max_retain=max_retain,
        )
        if item.decision == "retain":
            retained_count += 1
        decisions.append(item)

    recall_gaps = [
        f"missing expected source lane: {lane}"
        for lane in expected_lanes
        if lane not in observed_lanes
    ]
    has_retained = any(item.decision == "retain" for item in decisions)
    needs_broaden_or_deepen = bool(
        any(item.decision == "deepen" for item in decisions)
        or (formal_request and bool(recall_gaps))
        or (deep_request and not has_retained)
        or (not has_retained and bool(recall_gaps))
    )
    recommended_action = "synthesize_from_retained_sources"
    if needs_broaden_or_deepen:
        recommended_action = "broaden_or_deepen_before_final_synthesis"
    elif not decisions:
        recommended_action = "run_retrieval_before_synthesis"

    return SourceTriageResult(
        request_text=request,
        agent_name=agent,
        expected_lanes=list(expected_lanes),
        decisions=decisions,
        retained_source_ids=[item.source_id for item in decisions if item.decision == "retain"],
        retained_urls=[item.url for item in decisions if item.decision == "retain" and item.url],
        review_source_ids=[item.source_id for item in decisions if item.decision == "review"],
        review_urls=[item.url for item in decisions if item.decision == "review" and item.url],
        rejected_source_ids=[item.source_id for item in decisions if item.decision == "reject"],
        rejected_urls=[item.url for item in decisions if item.decision == "reject" and item.url],
        deepen_source_ids=[item.source_id for item in decisions if item.decision == "deepen"],
        deepen_urls=[item.url for item in decisions if item.decision == "deepen" and item.url],
        recall_gaps=recall_gaps,
        needs_broaden_or_deepen=needs_broaden_or_deepen,
        recommended_action=recommended_action,
    )


def _triage_one_candidate(
    candidate: SourceTriageCandidate,
    *,
    request_text: str,
    focus_terms: set[str],
    expected_lanes: tuple[str, ...],
    formal_request: bool,
    deep_request: bool,
    retained_count: int,
    max_retain: int,
) -> SourceTriageItem:
    haystack = " ".join([candidate.title, candidate.url, candidate.snippet]).lower()
    matched_terms = sorted(term for term in focus_terms if term in haystack)
    relevance_score = _score_relevance(focus_terms=focus_terms, matched_terms=matched_terms)
    expected_lane_hit = bool(set(candidate.lanes) & set(expected_lanes)) if expected_lanes else True
    directness_score = 75 if expected_lane_hit else 45
    if candidate.url and candidate.url.startswith(("http://", "https://")):
        directness_score += 10
    evidence_ready = _candidate_has_read_evidence(candidate)
    if evidence_ready:
        directness_score += 10
    elif candidate.snippet:
        directness_score += 5
    directness_score = max(0, min(100, directness_score))

    reasons: list[str] = []
    contradiction = bool(_SOURCE_CONTRADICTION_RE.search(haystack))
    if not candidate.url:
        reasons.append("missing source URL")
    if matched_terms:
        reasons.append("matches request focus: " + ", ".join(matched_terms[:5]))
    else:
        reasons.append("low request-term overlap")
    if expected_lanes and not expected_lane_hit:
        reasons.append("does not match expected source lanes: " + ", ".join(expected_lanes))
    if relevance_score >= 50 and not expected_lane_hit and not formal_request:
        reasons.append("strong request match despite incomplete source-lane classification")
    if contradiction:
        reasons.append("source text contradicts the requested source type or actionability")
    formal_actionability = bool(_FORMAL_ACTIONABILITY_RE.search(haystack))
    recognition_only = bool(_FORMAL_RECOGNITION_ONLY_RE.search(haystack))
    if formal_request and recognition_only:
        reasons.append("recognition or awards coverage is not an application opportunity")
    if formal_request and not formal_actionability:
        reasons.append(
            "missing concrete application, deadline, solicitation, eligibility, or funding path"
        )
    if evidence_ready:
        reasons.append("selected source has extracted/read evidence for synthesis")
    elif deep_request:
        reasons.append("source is not yet read/extracted for detailed synthesis")

    decision: SourceTriageDecision
    if formal_request and recognition_only:
        decision = "reject"
    elif contradiction and formal_request:
        decision = "reject"
    elif not candidate.url:
        decision = "reject"
    elif formal_request and not formal_actionability and expected_lane_hit:
        decision = "deepen" if deep_request and relevance_score >= 35 else "review"
        reasons.append("formal opportunity candidate needs page extraction before retention")
    elif deep_request and relevance_score >= 35 and not evidence_ready:
        decision = "deepen"
        reasons.append("promising match needs page extraction before detailed synthesis")
    elif deep_request and evidence_ready and expected_lane_hit and relevance_score >= 20:
        decision = "retain"
        reasons.append("read source in an expected lane has enough request overlap for synthesis")
    elif relevance_score < 20 and expected_lanes and not expected_lane_hit:
        decision = "reject"
    elif retained_count >= max(1, max_retain):
        decision = "review"
        reasons.append("retention cap reached")
    elif relevance_score >= 35 and expected_lane_hit:
        decision = "retain"
    elif relevance_score >= 50 and not formal_request:
        decision = "retain"
    else:
        decision = "review"

    return SourceTriageItem(
        source_id=candidate.source_id,
        title=candidate.title,
        url=candidate.url,
        decision=decision,
        relevance_score=relevance_score,
        directness_score=directness_score,
        lanes=candidate.lanes,
        reasons=list(dict.fromkeys(reasons)),
        rationale=_rationale(decision=decision, reasons=reasons),
    )


def _candidate_from_any(value: Any, *, index: int) -> SourceTriageCandidate:
    mapping = result_mapping(value)
    url = result_url(mapping)
    title = str(mapping.get("title") or "").strip()
    snippet = str(mapping.get("snippet") or mapping.get("content") or "").strip()
    source_type = str(mapping.get("source_type") or "").strip()
    key_facts = _string_list(mapping.get("key_facts"))
    supported_claims = _string_list(mapping.get("supported_claims"))
    evidence_excerpt = str(mapping.get("evidence_excerpt") or "").strip()
    if not snippet:
        snippet = " ".join([evidence_excerpt, *key_facts[:3], *supported_claims[:3]]).strip()
    lanes = list(
        classify_source_lanes(
            url=url,
            title=title,
            snippet=snippet,
            source_type=source_type,
        )
    )
    return SourceTriageCandidate(
        source_id=str(mapping.get("source_id") or f"source:{index}"),
        title=title,
        url=url,
        snippet=snippet,
        source=str(mapping.get("source") or "").strip(),
        source_type=source_type,
        published_at=mapping.get("published_at") or mapping.get("date"),
        lanes=lanes,
        extraction_status=str(mapping.get("extraction_status") or "").strip(),
        evidence_excerpt=evidence_excerpt,
        key_facts=key_facts,
        supported_claims=supported_claims,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _candidate_has_read_evidence(candidate: SourceTriageCandidate) -> bool:
    status = candidate.extraction_status.strip().lower()
    if status in {
        "article_read",
        "extracted",
        "page_read",
        "rendered_page_read",
        "success",
        "website_extracted",
    }:
        return bool(
            candidate.evidence_excerpt.strip()
            or candidate.key_facts
            or candidate.supported_claims
            or len(candidate.snippet.strip()) >= 240
        )
    if status in {
        "",
        "not_enabled",
        "not_requested",
        "search_result",
        "snippet_only",
        "source_linked",
    }:
        return False
    return bool(
        candidate.evidence_excerpt.strip() or candidate.key_facts or candidate.supported_claims
    )


def _expected_lanes(*, request_text: str, agent_name: str) -> tuple[str, ...]:
    if agent_name in {"opportunity_scout", "orchestrator"} or _FORMAL_REQUEST_RE.search(
        request_text
    ):
        return required_source_lanes_for_opportunity(request_text=request_text)
    if agent_name in {"business_research_analyst", "chief_of_staff"}:
        return required_source_lanes_for_company(request_text=request_text)
    return ()


def _focus_terms(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9-]{2,}", text.lower())
    return {
        token
        for token in tokens
        if token not in _STOPWORDS
        and not token.isdigit()
        and token not in {"exa", "tavily", "searxng", "metadata", "provider", "providers"}
    }


def _score_relevance(*, focus_terms: set[str], matched_terms: Sequence[str]) -> int:
    if not focus_terms:
        return 50
    return min(100, round(100 * (len(set(matched_terms)) / max(1, len(focus_terms)))))


def _rationale(*, decision: SourceTriageDecision, reasons: Sequence[str]) -> str:
    reason_text = "; ".join(str(reason) for reason in reasons[:3] if str(reason).strip())
    return f"{decision}: {reason_text}".strip()


__all__ = [
    "SourceTriageCandidate",
    "SourceTriageDecision",
    "SourceTriageItem",
    "SourceTriageResult",
    "SourceTriageSummary",
    "SourceTriageSummaryItem",
    "triage_source_candidates",
]
