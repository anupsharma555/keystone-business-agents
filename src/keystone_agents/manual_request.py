"""Pre-execution planning for manual Keystone agent calls."""

from __future__ import annotations

import re
from typing import Any

from keystone_agents.orchestrator.routing import (
    OPPORTUNITY_RE,
    OUTREACH_RE,
    RESUME_RE,
    SEND_RE,
    looks_like_company,
    looks_like_email,
    payload_text,
)
from keystone_agents.schemas.manual_request_plan import (
    ManualRequestIntent,
    ManualRequestPlan,
    ManualTargetAgent,
    ManualTargetType,
)
from keystone_agents.zotero_research import (
    extract_zotero_article_query,
    extract_zotero_collection_hint,
    looks_like_zotero_article_request,
    looks_like_zotero_collection_request,
)

_AGENT_ALIASES: dict[str, ManualTargetAgent] = {
    "orchestrator": "orchestrator",
    "orchestrator agent": "orchestrator",
    "business research analyst": "business_research_analyst",
    "business agent analyst": "business_research_analyst",
    "account researcher": "business_research_analyst",
    "company research agent": "business_research_analyst",
    "company research": "business_research_analyst",
    "opportunity scout": "opportunity_scout",
    "scout agent": "opportunity_scout",
    "gmail triage": "gmail_triage",
    "triage agent": "gmail_triage",
    "outreach composer": "outreach_composer",
    "outreach agent": "outreach_composer",
    "chief of staff": "chief_of_staff",
    "chief of staff agent": "chief_of_staff",
    "kni chief of staff": "chief_of_staff",
    "slack operations": "chief_of_staff",
    "slack ops": "chief_of_staff",
}
_ROUTE_INTENT: dict[ManualTargetAgent, ManualRequestIntent] = {
    "business_research_analyst": "company_research",
    "opportunity_scout": "opportunity_search",
    "gmail_triage": "gmail_triage",
    "outreach_composer": "outreach_draft",
    "chief_of_staff": "slack_operations",
    "orchestrator": "route_request",
    "clarification": "clarification",
}
_ROUTE_TARGET_TYPE: dict[ManualTargetAgent, ManualTargetType] = {
    "business_research_analyst": "company",
    "opportunity_scout": "topic",
    "gmail_triage": "gmail_thread",
    "outreach_composer": "company",
    "chief_of_staff": "slack_channel",
    "orchestrator": "unknown",
    "clarification": "unknown",
}
_COUNT_RE = re.compile(
    r"\b(?:find|return|list|top|show|identify|source)\s+(?P<count>\d{1,2})\b"
    r"|\b(?P<count2>\d{1,2})\s+"
    r"(?:opportunities|companies|institutes|researchers|conferences|people|leads|emails)\b",
    re.I,
)
_PREFIX_RE = re.compile(
    r"^(?:research|profile|evaluate|assess|summarize|look into|check out|"
    r"draft outreach to|draft email to|write outreach to|company research)\s+",
    re.I,
)
_OPPORTUNITY_TO_OUTREACH_RE = re.compile(
    r"\bopportunit(?:y|ies)\s*(?:-|to\s+)?outreach\b"
    r"|"
    r"\bopportunit(?:y|ies)\b.*\boutreach\b.*\b(loop|draft|email|approval|collaboration)\b"
    r"|"
    r"\boutreach\b.*\bopportunit(?:y|ies)\b.*\b(loop|draft|email|approval|collaboration)\b",
    re.I,
)
_LOOP_TOPIC_STOP_RE = re.compile(
    r"\s*(?:[.;]\s*)?(?:top\s+\d+|post\s+approval|request\s+approval|approval\s+to|"
    r"draft\s+only|do\s+not\s+send|don't\s+send|save\b|send\b).*$",
    re.I,
)
_REFERENCE_URL_RE = re.compile(r"https?://[^\s<>)]+", re.I)
_BROWSER_DIAGNOSTICS_RE = re.compile(
    r"\b(?:backend\s+browser|browser\s+diagnostics|rendered[- ]page|"
    r"render\s+page|console|network|playwright|lighthouse|devtools|"
    r"frontend|page\s+diagnos(?:e|is|tic)|browser\s+check)\b",
    re.I,
)
_OPTIONAL_DIAGNOSTICS_RE = re.compile(r"\b(?:only\s+if\s+needed|if\s+needed|fallback)\b", re.I)
_RESEARCH_ACTION_RE = re.compile(
    r"\b(?:research|profile|explain\s+whether|assess\s+whether|operating\s+company|"
    r"partnership|advisory|source-backed)\b",
    re.I,
)


def normalize_manual_agent(value: str | None) -> ManualTargetAgent | None:
    """Normalize human-facing agent names to canonical route names."""

    text = " ".join(str(value or "").replace("_", " ").lower().split())
    if not text:
        return None
    return _AGENT_ALIASES.get(text) or (
        text.replace(" ", "_") if text.replace(" ", "_") in _ROUTE_INTENT else None
    )


def infer_manual_request_plan(
    request: str | dict[str, Any] | None,
    *,
    requested_agent: str | None = None,
    source: str = "heuristic",
) -> ManualRequestPlan:
    """Infer a bounded semantic plan without model execution."""

    text = payload_text(request)
    normalized_agent = normalize_manual_agent(requested_agent)
    desired_count = _desired_count(text)
    target_agent = _semantic_target_agent(request, text, requested_agent=normalized_agent)
    workflow_allowed = normalized_agent in {None, "orchestrator"}
    intent = _intent_for_target(target_agent, text, workflow_allowed=workflow_allowed)
    plan = ManualRequestPlan(
        source=source,
        requested_agent=normalized_agent,
        target_agent=target_agent,
        intent=intent,
        primary_target=_primary_target(text, target_agent=target_agent),
        target_type=_target_type(text, target_agent=target_agent),
        objective=_objective(text, intent=intent),
        desired_count=desired_count,
        constraints=_constraints(text),
        gmail_query=_gmail_query(text) if target_agent == "gmail_triage" else "",
        lookback_days=_lookback_days(text) if target_agent == "gmail_triage" else None,
        draft_policy=_draft_policy(text) if target_agent == "gmail_triage" else "",
        recipient=_recipient(text) if target_agent == "outreach_composer" else "",
        outreach_channel=_outreach_channel(text) if target_agent == "outreach_composer" else "",
        tone=_tone(text) if target_agent == "outreach_composer" else "",
        requires_live_search=target_agent in {"business_research_analyst", "opportunity_scout"},
        requires_approved_context=target_agent == "outreach_composer",
        side_effect_policy="draft_or_read_only",
        rationale="Local semantic planner inferred the manual request before agent execution.",
    )
    if target_agent == "clarification":
        plan.planner_warnings.append(
            "Manual request did not contain enough information for a safe route."
        )
    if SEND_RE.search(text.lower()):
        plan.planner_warnings.append(
            "Send request blocked; Keystone manual agents are draft/read-only."
        )
    return plan


def merge_manual_request_plan(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan | dict[str, Any] | None,
) -> ManualRequestPlan:
    """Merge an LLM plan over the local fallback while preserving safety defaults."""

    if candidate is None:
        return base
    plan = (
        candidate
        if isinstance(candidate, ManualRequestPlan)
        else ManualRequestPlan.model_validate(candidate)
    )
    preserved_intents = {"opportunity_to_outreach_loop", "browser_diagnostics"}
    if base.intent in preserved_intents and (
        plan.intent != base.intent or plan.target_agent != base.target_agent
    ):
        warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
        warnings.append(
            "Ignored planner override that converted a protected manual request "
            "into a different route."
        )
        return base.model_copy(
            update={
                "source": plan.source or base.source,
                "planner_warnings": warnings,
            }
        )
    merged = base.model_copy(update=plan.model_dump(mode="json"))
    if not merged.requested_agent:
        merged.requested_agent = base.requested_agent
    if not merged.primary_target:
        merged.primary_target = base.primary_target
    if not merged.objective:
        merged.objective = base.objective
    if not merged.constraints:
        merged.constraints = list(base.constraints)
    if not merged.gmail_query:
        merged.gmail_query = base.gmail_query
    if merged.lookback_days is None:
        merged.lookback_days = base.lookback_days
    if not merged.draft_policy:
        merged.draft_policy = base.draft_policy
    if not merged.recipient:
        merged.recipient = base.recipient
    if not merged.outreach_channel:
        merged.outreach_channel = base.outreach_channel
    if not merged.tone:
        merged.tone = base.tone
    merged.desired_count = max(1, min(10, merged.desired_count or base.desired_count))
    if base.target_agent == "outreach_composer" or merged.target_agent == "outreach_composer":
        merged.requires_approved_context = True
    merged.side_effect_policy = "draft_or_read_only"
    return merged


def _semantic_target_agent(
    request: str | dict[str, Any] | None,
    text: str,
    *,
    requested_agent: ManualTargetAgent | None,
) -> ManualTargetAgent:
    lower = text.lower()
    if _looks_like_browser_diagnostics_only_request(text):
        return (
            requested_agent
            if requested_agent in {"chief_of_staff", "orchestrator"}
            else "chief_of_staff"
        )
    if requested_agent and requested_agent != "orchestrator":
        return requested_agent
    if _looks_like_slack_operations_request(lower):
        return "chief_of_staff"
    if looks_like_opportunity_to_outreach_loop(text):
        return "opportunity_scout"
    if SEND_RE.search(lower):
        return "clarification"
    if RESUME_RE.search(lower):
        return "orchestrator"
    if looks_like_zotero_article_request(text) or looks_like_zotero_collection_request(text):
        return "business_research_analyst"
    if OUTREACH_RE.search(lower):
        return "outreach_composer"
    if looks_like_email(request, text):
        return "gmail_triage"
    if OPPORTUNITY_RE.search(lower):
        return "opportunity_scout"
    if looks_like_company(text):
        return "business_research_analyst"
    return "clarification"


def _intent_for_target(
    target_agent: ManualTargetAgent,
    text: str,
    *,
    workflow_allowed: bool = True,
) -> ManualRequestIntent:
    lower = text.lower()
    if _looks_like_browser_diagnostics_only_request(text):
        return "browser_diagnostics"
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(lower):
        return "reference_capture"
    if (
        workflow_allowed
        and target_agent == "opportunity_scout"
        and looks_like_opportunity_to_outreach_loop(text)
    ):
        return "opportunity_to_outreach_loop"
    if SEND_RE.search(lower):
        return "blocked_send"
    if RESUME_RE.search(lower):
        return "continue_work_item"
    if target_agent == "business_research_analyst" and "zotero" in lower:
        return "research_brief"
    return _ROUTE_INTENT.get(target_agent, "clarification")


def _looks_like_slack_operations_request(lower: str) -> bool:
    if "chief of staff" in lower or "slack ops" in lower or "slack operations" in lower:
        return True
    return bool(
        "slack" in lower
        and any(
            term in lower
            for term in (
                "channel",
                "route",
                "routing",
                "calendar",
                "gmail",
                "meeting",
                "onboarding",
                "socket",
                "post",
            )
        )
    )


def _looks_like_reference_capture_request(lower: str) -> bool:
    markers = (
        "keep this for future reference",
        "for future reference",
        "remember this",
        "save this",
        "save for later",
        "bookmark this",
        "note this",
        "store this",
        "add this to memory",
        "keep this",
    )
    if any(marker in lower for marker in markers):
        return True
    return bool(_REFERENCE_URL_RE.search(lower)) and any(
        marker in lower for marker in ("remember", "reference", "bookmark", "save")
    )


def _looks_like_browser_diagnostics_request(text: str) -> bool:
    cleaned = str(text or "")
    lower = cleaned.lower()
    if not _BROWSER_DIAGNOSTICS_RE.search(cleaned):
        return False
    if _REFERENCE_URL_RE.search(cleaned) or "localhost" in lower or "127.0.0.1" in lower:
        return True
    return any(
        marker in lower
        for marker in (
            "backend browser",
            "browser diagnostics",
            "playwright",
            "rendered-page",
            "rendered page",
            "devtools",
            "lighthouse",
        )
    ) or ("console" in lower and "page" in lower)


def _looks_like_browser_diagnostics_only_request(text: str) -> bool:
    if not _looks_like_browser_diagnostics_request(text):
        return False
    if _OPTIONAL_DIAGNOSTICS_RE.search(text) and _RESEARCH_ACTION_RE.search(text):
        return False
    return True


def _desired_count(text: str) -> int:
    match = _COUNT_RE.search(text)
    if match is None:
        return 1
    raw = match.group("count") or match.group("count2")
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return 1


def _primary_target(text: str, *, target_agent: ManualTargetAgent) -> str:
    cleaned = _strip_direct_agent_prefix(text).strip()
    if _looks_like_browser_diagnostics_request(cleaned):
        url_match = _REFERENCE_URL_RE.search(cleaned)
        return url_match.group(0).rstrip(".,;") if url_match else cleaned[:120]
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(cleaned.lower()):
        return _reference_target(cleaned)
    if target_agent == "business_research_analyst":
        zotero_query = (
            extract_zotero_article_query(cleaned)
            if looks_like_zotero_article_request(cleaned)
            else ""
        )
        if zotero_query:
            return zotero_query
        zotero_hint = extract_zotero_collection_hint(cleaned)
        if zotero_hint:
            return zotero_hint
    if target_agent == "opportunity_scout":
        if looks_like_opportunity_to_outreach_loop(cleaned):
            return _opportunity_to_outreach_topic(cleaned)
        return _first_nonempty(
            _quoted_text(cleaned), _opportunity_search_target(cleaned), cleaned[:120]
        )
    cleaned = _PREFIX_RE.sub("", _strip_operational_clauses(cleaned)).strip()
    if target_agent == "gmail_triage":
        return _first_nonempty(_quoted_text(cleaned), _subject_text(cleaned), cleaned[:120])
    if target_agent == "outreach_composer":
        return _first_nonempty(_quoted_text(cleaned), _companyish_target(cleaned), cleaned[:120])
    if target_agent == "business_research_analyst":
        return _first_nonempty(_quoted_text(cleaned), _companyish_target(cleaned), cleaned[:120])
    return _first_nonempty(_quoted_text(cleaned), cleaned[:120])


def _opportunity_search_target(text: str) -> str:
    cleaned = _strip_operational_clauses(text)
    cleaned = re.sub(r"https?://\S+|www\.\S+", " ", cleaned)
    cleaned = re.sub(
        r"^\s*(?:please\s+)?(?:find|identify|source|search\s+for|look\s+for|list|return|show)\s+",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^\s*(?:top\s+)?\d{1,2}\s+", " ", cleaned, flags=re.I)
    cleaned = re.sub(
        r"\b(?:opportunit(?:y|ies)|leads|targets)\b\s*$",
        "opportunities",
        cleaned,
        flags=re.I,
    )
    cleaned = " ".join(cleaned.split()).strip(" .,:;-")
    return cleaned[:160]


def _strip_operational_clauses(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    cleaned = re.split(
        r"(?:^|\s+)(?:"
        r"use\s+live\b|"
        r"live\s+sdk\b|"
        r"live\s+search\b|"
        r"no\s+outreach\b|"
        r"no\s+gmail\b|"
        r"no\s+external\s+writes\b|"
        r"do\s+not\s+(?:send|email|post|write|modify|delete|share)\b|"
        r"draft\s+only\b|"
        r"test\b|"
        r"keep\s+the\s+output\b"
        r")",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]
    return cleaned.strip(" .,:;-")


def _target_type(text: str, *, target_agent: ManualTargetAgent) -> ManualTargetType:
    lower = text.lower()
    if _looks_like_browser_diagnostics_request(text):
        return "url"
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(lower):
        return "operator_reference"
    if target_agent == "business_research_analyst":
        if looks_like_zotero_article_request(text):
            return "zotero_article"
        if looks_like_zotero_collection_request(text):
            return "zotero_collection"
        if "article collection" in lower or "paper collection" in lower:
            return "article_collection"
        if any(marker in lower for marker in ("institute", "center", "program", "lab")):
            return "institute"
        if any(marker in lower for marker in ("conference", "symposium", "summit")):
            return "conference"
        if any(
            marker in lower
            for marker in ("person", "researcher", "principal investigator", "faculty")
        ):
            return "person"
        if "topic" in lower:
            return "topic"
        return "company"
    if target_agent == "opportunity_scout":
        if looks_like_opportunity_to_outreach_loop(text):
            return "opportunity"
        if any(marker in lower for marker in ("conference", "symposium", "summit")):
            return "conference"
        if any(marker in lower for marker in ("researcher", "principal investigator", "faculty")):
            return "person"
        if any(marker in lower for marker in ("institute", "center", "program", "lab")):
            return "institute"
        if any(marker in lower for marker in ("company", "companies", "startup", "vendor")):
            return "company"
        return "topic"
    return _ROUTE_TARGET_TYPE.get(target_agent, "unknown")


def _reference_target(text: str) -> str:
    url_match = _REFERENCE_URL_RE.search(text)
    cleaned = _REFERENCE_URL_RE.sub(" ", text)
    cleaned = re.sub(
        r"\b(please\s+)?(keep|remember|save|bookmark|note|store|add)\b",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\b(this|for|future|reference|later|to|memory)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[:\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        return cleaned[:120]
    return url_match.group(0)[:120] if url_match else ""


def _objective(text: str, *, intent: ManualRequestIntent) -> str:
    cleaned = _strip_direct_agent_prefix(text).strip()
    if intent == "browser_diagnostics":
        return cleaned or "Run read-only backend browser diagnostics."
    if intent == "opportunity_to_outreach_loop":
        return cleaned or "Run the opportunity-to-outreach loop and queue draft-only approval."
    if intent == "company_research":
        return cleaned or "Prepare a concise source-backed research brief."
    if intent == "research_brief":
        return cleaned or "Prepare a concise source-backed research brief."
    if intent == "opportunity_search":
        return cleaned or "Find source-backed Keystone-relevant opportunities."
    if intent == "gmail_triage":
        return cleaned or "Triage the requested Gmail scope without sending email."
    if intent == "outreach_draft":
        return cleaned or "Draft approval-gated outreach from approved context."
    if intent == "blocked_send":
        return "Block external send side effects and ask whether draft-only output is desired."
    return cleaned


def _constraints(text: str) -> list[str]:
    lower = text.lower()
    constraints: list[str] = []
    for marker in (
        "u.s.",
        "us-relevant",
        "current",
        "recent",
        "behavioral health",
        "digital mental health",
        "psychiatry",
        "clinical ai",
        "neuroinformatics",
        "evidence-generation",
        "implementation",
        "conference",
        "research collaboration",
        "advisory",
    ):
        if marker in lower:
            constraints.append(marker)
    return constraints


def looks_like_opportunity_to_outreach_loop(text: str) -> bool:
    """Return whether text asks for the integrated opportunity -> outreach workflow."""

    return bool(_OPPORTUNITY_TO_OUTREACH_RE.search(str(text or "")))


def _strip_direct_agent_prefix(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    lowered = cleaned.lower()
    for alias in sorted(_AGENT_ALIASES, key=len, reverse=True):
        if lowered.startswith(alias + " "):
            return cleaned[len(alias) :].strip()
    return cleaned


def _opportunity_to_outreach_topic(text: str) -> str:
    cleaned = _strip_direct_agent_prefix(text).strip()
    match = re.search(r"\bfor\s+(?P<topic>.+)$", cleaned, re.I)
    if match:
        candidate = match.group("topic").strip()
    else:
        candidate = re.sub(
            r"^\s*(?:run|start|do|execute|find|source|identify)\s+"
            r"(?:one\s+)?(?:opportunit(?:y|ies)\s*(?:-|to\s+)?outreach\s+"
            r"(?:loop|workflow|process)?|loop)\s*",
            "",
            cleaned,
            flags=re.I,
        ).strip()
    candidate = _LOOP_TOPIC_STOP_RE.sub("", candidate).strip(" .,:;-")
    return candidate or cleaned[:120]


def _companyish_target(text: str) -> str:
    without_url = re.sub(r"https?://\S+|www\.\S+", "", text).strip()
    parts = re.split(r"\b(?:for|about|where|with|using|to)\b", without_url, maxsplit=1, flags=re.I)
    candidate = parts[0].strip(" :,-")
    capitalized = re.match(
        r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b",
        candidate,
    )
    if capitalized:
        return capitalized.group("name").strip(" :,-")
    candidate = re.sub(
        r"\b(?:recent|current|source-backed|concise|brief|company)\b", "", candidate, flags=re.I
    )
    candidate = " ".join(candidate.split()).strip(" :,-")
    if 1 <= len(candidate.split()) <= 6:
        return candidate
    return ""


def _quoted_text(text: str) -> str:
    match = re.search(r"[\"“](.*?)[\"”]", text)
    return match.group(1).strip() if match else ""


def _subject_text(text: str) -> str:
    match = re.search(r"\bsubject:\s*(.+)", text, re.I)
    return match.group(1).strip()[:120] if match else ""


def _first_nonempty(*values: str) -> str:
    for value in values:
        cleaned = " ".join(str(value or "").split()).strip()
        if cleaned:
            return cleaned
    return ""


def _lookback_days(text: str) -> int | None:
    match = re.search(r"\b(?:last|past)\s+(?P<days>\d{1,3})\s+days?\b", text, re.I)
    if match is None:
        return None
    try:
        return max(1, min(365, int(match.group("days"))))
    except (TypeError, ValueError):
        return None


def _draft_policy(text: str) -> str:
    lower = text.lower()
    if "draft" not in lower and "reply" not in lower:
        return "no_drafts_requested"
    if "urgent" in lower:
        return "draft_only_for_urgent"
    return "draft_only_when_reply_needed"


def _gmail_query(text: str) -> str:
    lower = text.lower()
    parts: list[str] = []
    if "unread" in lower:
        parts.append("is:unread")
    if "inbox" in lower:
        parts.append("in:inbox")
    days = _lookback_days(text)
    if days is not None:
        parts.append(f"newer_than:{days}d")
    return " ".join(parts)


def _recipient(text: str) -> str:
    match = re.search(r"\b(?:to|for)\s+([A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,5})\b", text)
    return match.group(1).strip() if match else ""


def _outreach_channel(text: str) -> str:
    lower = text.lower()
    if "linkedin" in lower:
        return "linkedin"
    if "email" in lower:
        return "email"
    return "email"


def _tone(text: str) -> str:
    lower = text.lower()
    if "warm" in lower:
        return "warm_professional"
    if "concise" in lower or "brief" in lower:
        return "concise"
    if "formal" in lower:
        return "formal"
    return ""
