"""Recommendation intake and generalized opportunity qualification."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from keystone_agents.agents.orchestrator import route_request
from keystone_agents.memory import opportunity_entity_memory_items
from keystone_agents.schemas.recommendation import (
    OpportunityContactPath,
    OpportunityEntity,
    OpportunityEntityType,
    RecommendationDecision,
    RecommendationIntakeResult,
    RecommendationNextStep,
    RecommendationScore,
)
from keystone_agents.tools.storage_tool import StorageTool

_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|ai|io|health)\b", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
_LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/\S+", re.I)
_KEYSTONE_TERMS = {
    "clinical ai",
    "evidence",
    "validation",
    "psychiatry",
    "behavioral health",
    "mental health",
    "neuroscience",
    "cns",
    "clinical trial",
    "research operations",
    "neuroinformatics",
    "digital health",
}
_TIMING_TERMS = {
    "deadline",
    "rfa",
    "rfp",
    "grant",
    "hiring",
    "launch",
    "conference",
    "abstract",
    "submissions",
    "funding",
    "new",
    "recent",
    "2026",
}


def qualify_recommendation(
    recommendation: str | Mapping[str, Any],
    *,
    source_links: Sequence[str] | None = None,
    live_search: bool = False,
) -> RecommendationIntakeResult:
    """Classify and score an operator-provided recommendation without side effects."""

    text = _recommendation_text(recommendation)
    explicit_links = list(source_links or [])
    explicit_links.extend(_extract_links(text))
    entity = _extract_entity(text, explicit_links=explicit_links)
    score = _score_recommendation(text=text, entity=entity, live_search=live_search)
    decision = _decision_from_score(score)
    next_step = _next_step_from_decision(decision, entity)
    unknowns = _unknowns(text=text, entity=entity, live_search=live_search)
    risks = _risks(text=text, entity=entity, score=score)
    suggested_searches = _suggested_searches(entity)

    return RecommendationIntakeResult(
        input_text=text,
        entity=entity,
        decision=decision,
        recommended_next_step=next_step,
        score=score,
        why_relevant=_why_relevant(text, entity),
        evidence_summary=_evidence_summary(
            text=text,
            entity=entity,
            live_search=live_search,
        ),
        unknowns=unknowns,
        risks=risks,
        suggested_searches=suggested_searches,
        source_links=list(dict.fromkeys(explicit_links)),
    )


def save_recommendation_intake_result(
    result: RecommendationIntakeResult,
    *,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Persist recommendation intake output and generalized entity memory."""

    storage = StorageTool(
        database_url=database_url,
        agent_name="recommendation_intake",
        dry_run=True,
    )
    run = storage.save_agent_run(
        agent_name="recommendation_intake",
        input_payload={"recommendation": result.input_text},
        input_summary=result.input_text[:240],
        output=result.model_dump(mode="json"),
        model="fixture",
        dry_run=True,
        status="success",
    )
    entity_memory_payload = {
        "entity_name": result.entity.name,
        "entity_kind": result.entity.entity_type,
        "canonical_entity_key": result.entity.canonical_key,
        "summary": result.why_relevant or result.entity.relevance_summary,
        "description": result.entity.description,
        "priority_score": result.score.priority_score,
        "keystone_fit_reason": result.why_relevant,
        "recommended_next_step": result.recommended_next_step,
        "research_needed": result.unknowns,
        "source_ids": result.entity.source_ids,
        "source_urls": result.entity.source_urls,
    }
    memory_ids = [
        storage.save_memory_item(item.model_dump(mode="json"))["id"]
        for item in opportunity_entity_memory_items(entity_memory_payload)
    ]
    return {
        "agent_run": run,
        "memory_ids": memory_ids,
        "send_enabled": False,
    }


def recommendation_intake_markdown(result: RecommendationIntakeResult) -> str:
    """Render a compact human review packet for a recommendation intake result."""

    entity = result.entity
    lines = [
        "# Recommendation Intake",
        "",
        "## Decision",
        f"- Decision: {result.decision}",
        f"- Recommended next step: {result.recommended_next_step}",
        f"- Priority score: {result.score.priority_score}/100",
        f"- Rationale: {result.score.rationale}",
        "",
        "## Entity",
        f"- Type: {entity.entity_type}",
        f"- Name: {entity.name}",
        f"- URL: {entity.url or 'Needs research'}",
        f"- Relevance: {entity.relevance_summary or result.why_relevant}",
        f"- Timing: {entity.timing_signal or 'Not established'}",
        "",
        "## Contact Paths",
    ]
    if entity.contact_paths:
        lines.extend(
            f"- {path.path_type}: {path.value or path.url or 'Needs confirmation'}"
            for path in entity.contact_paths
        )
    else:
        lines.append("- Needs contact research.")
    lines.extend(
        [
            "",
            "## Evidence",
            f"- {result.evidence_summary or 'Needs source-backed evidence.'}",
        ]
    )
    if result.source_links:
        lines.append("- Source links:")
        lines.extend(f"  - {link}" for link in result.source_links[:8])
    if result.unknowns:
        lines.append("")
        lines.append("## Unknowns")
        lines.extend(f"- {item}" for item in result.unknowns)
    if result.risks:
        lines.append("")
        lines.append("## Risks")
        lines.extend(f"- {item}" for item in result.risks)
    lines.extend(
        [
            "",
            "## Suggested Searches",
            *[f"- {query}" for query in result.suggested_searches],
            "",
            "## Safety",
            "- Draft-only. No email, LinkedIn message, or external action is sent.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def recommendation_orchestrator_route(
    recommendation: str | Mapping[str, Any],
) -> dict[str, Any]:
    """Return how the current orchestrator routes a recommendation intake request."""

    result = route_request(
        {
            "request": _recommendation_text(recommendation),
            "intent": "recommendation_intake",
        }
    )
    return {
        "route": result.route,
        "target_agent": result.target_agent,
        "rationale": result.rationale,
        "approval_required": result.approval_required,
        "send_enabled": result.send_enabled,
    }


def _recommendation_text(value: str | Mapping[str, Any]) -> str:
    if isinstance(value, str):
        return value.strip()
    parts: list[str] = []
    for key in (
        "recommendation",
        "request",
        "input",
        "text",
        "name",
        "entity",
        "url",
        "notes",
    ):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return "\n".join(parts).strip()


def _extract_entity(text: str, *, explicit_links: Sequence[str]) -> OpportunityEntity:
    entity_type = _infer_entity_type(text)
    url = explicit_links[0] if explicit_links else ""
    name = _infer_entity_name(text, entity_type=entity_type, url=url)
    contact_paths = _contact_paths(text, explicit_links)
    return OpportunityEntity(
        entity_type=entity_type,
        name=name or "Recommended Opportunity",
        url=url,
        description=text[:500],
        relevance_summary=_first_sentence(text),
        timing_signal=_timing_signal(text),
        contact_paths=contact_paths,
        source_ids=[
            f"recommendation_source:{index}" for index, _link in enumerate(explicit_links, start=1)
        ],
        source_urls=list(explicit_links),
        confidence=0.65 if name else 0.4,
    )


def _infer_entity_type(text: str) -> OpportunityEntityType:
    lowered = text.lower()
    if "conference" in lowered or "symposium" in lowered or "summit" in lowered:
        return "conference"
    if "grant" in lowered:
        return "grant"
    if "rfp" in lowered or "request for proposal" in lowered:
        return "rfp"
    if "accelerator" in lowered:
        return "accelerator"
    if "institute" in lowered or "center" in lowered:
        return "institute"
    if " lab" in lowered or "laboratory" in lowered:
        return "lab"
    if "funder" in lowered or "foundation" in lowered:
        return "funder"
    if "person" in lowered or "individual" in lowered or "professor" in lowered:
        return "person"
    if "linkedin.com/in/" in lowered:
        return "person"
    if "publication" in lowered or "journal" in lowered:
        return "publication_group"
    if "company" in lowered or "startup" in lowered or "linkedin.com/company" in lowered:
        return "company"
    return "other"


def _infer_entity_name(text: str, *, entity_type: OpportunityEntityType, url: str) -> str:
    if isinstance(url, str) and url:
        host = re.sub(r"^https?://", "", url).split("/", 1)[0]
        label = host.removeprefix("www.").split(".", 1)[0]
        if label and label not in {"linkedin"}:
            return label.replace("-", " ").title()
    patterns = [
        (
            r"(?:recommend|consider|evaluate|review|contact|outreach to|reach out to)"
            r"\s+([A-Z][\w&.,' -]{2,80})"
        ),
        (
            r"(?:company|startup|institute|conference|grant|person|lab)[:\s]+"
            r"([A-Z][\w&.,' -]{2,80})"
        ),
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _trim_name(match.group(1), entity_type=entity_type)
    title_case = re.findall(r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,5}", text)
    if title_case:
        return _trim_name(title_case[0], entity_type=entity_type)
    return ""


def _trim_name(value: str, *, entity_type: OpportunityEntityType) -> str:
    cleaned = value.strip(" .,:;-")
    stop_words = {
        "for",
        "because",
        "about",
        "around",
        "regarding",
        "as",
        "if",
        "maybe",
    }
    parts = []
    for word in cleaned.split():
        if word.lower() in stop_words:
            break
        parts.append(word)
    result = " ".join(parts).strip(" .,:;-")
    if entity_type == "company":
        result = re.sub(r"\b(company|startup)\b$", "", result, flags=re.I).strip()
    return result[:100]


def _extract_links(text: str) -> list[str]:
    links = []
    for match in _URL_RE.findall(text):
        value = match.rstrip(").,;]")
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        links.append(value)
    return list(dict.fromkeys(links))


def _contact_paths(text: str, links: Sequence[str]) -> list[OpportunityContactPath]:
    paths: list[OpportunityContactPath] = []
    for email in _EMAIL_RE.findall(text):
        paths.append(
            OpportunityContactPath(
                path_type="email",
                value=email,
                confidence=0.9,
                needs_confirmation=False,
            )
        )
    for link in links:
        if _LINKEDIN_RE.search(link):
            path_type = "linkedin"
        elif "conference" in link.lower():
            path_type = "conference_portal"
        else:
            path_type = "website"
        paths.append(OpportunityContactPath(path_type=path_type, url=link, confidence=0.7))
    return paths


def _score_recommendation(
    *,
    text: str,
    entity: OpportunityEntity,
    live_search: bool,
) -> RecommendationScore:
    lowered = text.lower()
    fit_hits = sum(1 for term in _KEYSTONE_TERMS if term in lowered)
    timing_hits = sum(1 for term in _TIMING_TERMS if term in lowered)
    source_count = len(entity.source_urls) + len(entity.source_ids)
    contact_count = len(entity.contact_paths)
    fit = min(100, 25 + fit_hits * 14)
    timing = min(100, 25 + timing_hits * 12)
    evidence = min(100, 25 + source_count * 18 + (10 if live_search else 0))
    contactability = min(100, 20 + contact_count * 25)
    effort = 70 if entity.entity_type in {"company", "person"} else 55
    score = RecommendationScore(
        fit_score=fit,
        timing_score=timing,
        evidence_score=evidence,
        contactability_score=contactability,
        effort_score=effort,
        rationale=(
            "Scored from Keystone term fit, timing signals, source links, contact paths, "
            "and expected follow-up effort."
        ),
    )
    return score


def _decision_from_score(score: RecommendationScore) -> RecommendationDecision:
    if score.priority_score >= 75:
        return "pursue"
    if score.priority_score >= 55:
        return "maybe"
    if score.evidence_score < 45 or score.contactability_score < 45:
        return "needs_more_research"
    if score.fit_score >= 35:
        return "maybe"
    return "archive"


def _next_step_from_decision(
    decision: RecommendationDecision,
    entity: OpportunityEntity,
) -> RecommendationNextStep:
    if decision == "archive":
        return "archive"
    if decision == "needs_more_research":
        return "contact_research" if entity.entity_type == "person" else "account_research"
    if decision == "pursue" and any(path.path_type == "email" for path in entity.contact_paths):
        return "draft_email"
    if decision == "pursue" and any(path.path_type == "linkedin" for path in entity.contact_paths):
        return "draft_linkedin"
    return "account_research"


def _unknowns(*, text: str, entity: OpportunityEntity, live_search: bool) -> list[str]:
    unknowns: list[str] = []
    if not entity.source_urls and not live_search:
        unknowns.append("Need source-backed evidence beyond the operator recommendation.")
    if not entity.contact_paths:
        unknowns.append("Need a confirmed contact path before drafting outreach.")
    if not _timing_signal(text):
        unknowns.append("Need timing or why-now signal.")
    if entity.entity_type == "other":
        unknowns.append("Need entity type classification.")
    return unknowns


def _risks(*, text: str, entity: OpportunityEntity, score: RecommendationScore) -> list[str]:
    risks: list[str] = []
    if score.evidence_score < 50:
        risks.append("Weak evidence; run search/business research before outreach.")
    if entity.entity_type in {"person", "conference"} and not entity.contact_paths:
        risks.append("Contact route is not confirmed.")
    if "patient" in text.lower():
        risks.append("Possible sensitive or patient-specific context; review before use.")
    return risks


def _suggested_searches(entity: OpportunityEntity) -> list[str]:
    name = entity.name
    if not name:
        return []
    searches = [
        f"{name} official website",
        f"{name} clinical AI evidence research operations",
        f"{name} contact leadership email LinkedIn",
    ]
    if entity.entity_type in {"conference", "grant", "rfp"}:
        searches.append(f"{name} deadline submission eligibility")
    return searches


def _why_relevant(text: str, entity: OpportunityEntity) -> str:
    hits = [term for term in sorted(_KEYSTONE_TERMS) if term in text.lower()]
    if hits:
        return f"Recommendation mentions Keystone-relevant themes: {', '.join(hits[:5])}."
    return f"{entity.name} may be relevant, but Keystone-specific fit needs research."


def _evidence_summary(*, text: str, entity: OpportunityEntity, live_search: bool) -> str:
    if entity.source_urls:
        return f"Operator supplied {len(entity.source_urls)} source link(s)."
    if live_search:
        return (
            "Live search was requested; downstream research should attach source-backed evidence."
        )
    return "No external evidence has been attached yet."


def _timing_signal(text: str) -> str:
    lowered = text.lower()
    for term in sorted(_TIMING_TERMS):
        if term in lowered:
            return f"Mentions timing signal: {term}."
    return ""


def _first_sentence(text: str) -> str:
    stripped = " ".join(text.split())
    if not stripped:
        return ""
    return re.split(r"(?<=[.!?])\s+", stripped, maxsplit=1)[0][:280]
