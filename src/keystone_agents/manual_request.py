"""Pre-execution planning for manual Keystone agent calls."""

from __future__ import annotations

import re
from typing import Any

from keystone_agents.orchestrator.routing import (
    OPPORTUNITY_RE,
    OUTREACH_RE,
    looks_like_company,
    looks_like_email,
    looks_like_resume_request,
    looks_like_send_side_effect,
    payload_text,
)
from keystone_agents.schemas.manual_request_plan import (
    ManualExpectedArtifactType,
    ManualRequestIntent,
    ManualRequestPlan,
    ManualTargetAgent,
    ManualTargetType,
    ManualTaskObjective,
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
    "research analyst": "business_research_analyst",
    "analyst": "business_research_analyst",
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
    "airtable context agent": "airtable_context_agent",
    "airtable context": "airtable_context_agent",
    "airtable agent": "airtable_context_agent",
    "google workspace context agent": "google_workspace_context_agent",
    "google workspace context": "google_workspace_context_agent",
    "workspace context agent": "google_workspace_context_agent",
    "workspace context": "google_workspace_context_agent",
    "google drive context": "google_workspace_context_agent",
    "google docs context": "google_workspace_context_agent",
    "google sheets context": "google_workspace_context_agent",
    "zotero context agent": "zotero_context_agent",
    "zotero context": "zotero_context_agent",
    "zotero agent": "zotero_context_agent",
    "rss context agent": "rss_context_agent",
    "rss context": "rss_context_agent",
    "announcements context agent": "rss_context_agent",
    "announcements context": "rss_context_agent",
    "preprints context agent": "preprints_context_agent",
    "preprints context": "preprints_context_agent",
    "preprint context agent": "preprints_context_agent",
    "preprint context": "preprints_context_agent",
}
_ROUTE_INTENT: dict[ManualTargetAgent, ManualRequestIntent] = {
    "business_research_analyst": "company_research",
    "opportunity_scout": "opportunity_search",
    "gmail_triage": "gmail_triage",
    "outreach_composer": "outreach_draft",
    "chief_of_staff": "slack_operations",
    "airtable_context_agent": "context_lookup",
    "google_workspace_context_agent": "context_lookup",
    "zotero_context_agent": "context_lookup",
    "rss_context_agent": "context_lookup",
    "preprints_context_agent": "context_lookup",
    "orchestrator": "route_request",
    "clarification": "clarification",
}
_ROUTE_TARGET_TYPE: dict[ManualTargetAgent, ManualTargetType] = {
    "business_research_analyst": "company",
    "opportunity_scout": "topic",
    "gmail_triage": "gmail_thread",
    "outreach_composer": "company",
    "chief_of_staff": "slack_channel",
    "airtable_context_agent": "business_system_context",
    "google_workspace_context_agent": "business_system_context",
    "zotero_context_agent": "business_system_context",
    "rss_context_agent": "article_collection",
    "preprints_context_agent": "article_collection",
    "orchestrator": "unknown",
    "clarification": "unknown",
}
_CONTEXT_AGENT_TARGETS: frozenset[ManualTargetAgent] = frozenset(
    {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }
)
_COUNT_RE = re.compile(
    r"\b(?:compare|discover|find|return|list|top|show|identify|source)\s+"
    r"(?:up\s+to\s+)?(?P<count>\d{1,2})\b"
    r"|\b(?P<count2>\d{1,2})\s+"
    r"(?:[a-z][\w-]*\s+){0,4}"
    r"(?:opportunities|companies|institutes|researchers|conferences|people|leads|emails|"
    r"roles|jobs|positions|postings|openings)\b",
    re.I,
)
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_COUNT_WORD_RE = re.compile(
    r"\b(?:compare|discover|find|return|list|top|show|identify|source|best)\s+"
    r"(?:up\s+to\s+|the\s+)?"
    r"(?P<count_word>one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b(?P<count_word2>one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:[a-z][\w-]*\s+){0,4}"
    r"(?:opportunities|companies|institutes|researchers|conferences|people|leads|emails|"
    r"roles|jobs|positions|postings|openings)\b",
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
_NO_OUTREACH_DRAFT_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|no|without|avoid|skip)\s+"
    r"(?:create\s+|queue\s+|produce\s+)?"
    r"(?:draft|drafting|compose|write|prepare)\b.{0,50}\b(?:outreach|emails?|messages?)\b"
    r"|"
    r"\b(?:do\s+not|don't|dont|never|no|without|avoid|skip)\s+"
    r"(?:create\s+|queue\s+|produce\s+)?(?:outreach|email|message)\s+drafts?\b"
    r"|"
    r"\b(?:outreach|emails?|messages?)\b.{0,50}\b"
    r"(?:do\s+not|don't|dont|never|no|without|avoid|skip)\s+"
    r"(?:create\s+|queue\s+|produce\s+)?(?:draft|drafting|compose|write|prepare)\b",
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
_NO_EXTERNAL_RESEARCH_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|no|without|avoid|skip)\b"
    r"[^.;\n]{0,180}\b"
    r"(?:web\s+search|live\s+web|live\s+search|external\s+(?:search|research|tools?)|"
    r"browser\s+automation|research\s+externally)\b"
    r"|"
    r"\buse\s+only\s+(?:this\s+)?(?:approved\s+|sanitized\s+|provided\s+|"
    r"source-provided\s+|inline\s+)*"
    r"(?:inline\s+)?context\b",
    re.I,
)
_WORKFLOW_AGENT_MARKER_RE = re.compile(
    r"\b(?:coordinate|sequence|which\s+agents|agents?\s+should|routing\s+plan|"
    r"run\s+(?:the\s+)?workflow|multi[- ]agent|safe\s+parts\s+first|"
    r"score\s+the\s+workflow|approval\s+checklist|next\s+actions?|"
    r"plan\s+the\s+safest\s+workflow|decide\s+the\s+workflow|"
    r"what\s+should\s+the\s+agents\s+do)\b",
    re.I,
)
_WORKFLOW_RESEARCH_MARKER_RE = re.compile(
    r"\b(?:company\s+research|research\s+brief|research\s+summary|gmail\s+context|"
    r"opportunit(?:y|ies|y\s+scan)|partnership\s+angle|risk\s+and\s+uncertainty|"
    r"recommended\s+next\s+action|candidate\s+company|research\s+it|"
    r"find\s+companies|research\s+the\s+best\s+candidate)\b",
    re.I,
)
_TARGET_COMPANY_RE = re.compile(r"\btarget\s+company\s*:\s*(?P<name>[^\n.;]+)", re.I)
_COMPANY_NAME_RE = re.compile(r"\bcompany\s+name\s*:\s*(?P<name>[^\n.;]+)", re.I)
_COMPANY_LABEL_RE = re.compile(r"\bcompany\s*:\s*(?P<name>[^\n.;]+)", re.I)
_RESEARCH_ON_COMPANY_RE = re.compile(
    r"\b(?:brief|research|profile|summary)\s+on\s+"
    r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b"
)
_RESEARCH_COMPANY_ACTION_RE = re.compile(
    r"\b(?i:research|profile|analyze|investigate|assess|summarize)\s+"
    r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    r"\s+(?i:as|for|with|and|before|using|from|about|to|,|\.)\b"
)
_COMPANY_COMPARISON_RE = re.compile(
    r"\b(?:compare|comparison\s+of)\s+"
    r"(?P<company_a>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    r"\s+(?:and|vs\.?|versus)\s+"
    r"(?P<company_b>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b",
    re.I,
)
_COMPANY_WORTH_RE = re.compile(
    r"\bwhether\s+(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\s+"
    r"(?:is|are)\s+worth\b"
)
_COMPANY_LOOK_RE = re.compile(
    r"\b(?:take\s+(?:a\s+)?(?:quick\s+)?(?:read-only\s+)?look\s+at|"
    r"(?:quick\s+)?(?:read-only\s+)?look\s+at|fit\s+check\s+on|"
    r"check\s+on|review)\s+"
    r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    r"\s+(?i:as|for|with|and|before|using|from|about|to|,|\.)\b"
)
_FOR_COMPANY_RE = re.compile(
    r"\bfor\s+(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b"
)
_ROLE_DISCOVERY_RE = re.compile(
    r"\b(?:find|identify|search|scout|source|discover|list)\b[\s\S]*?"
    r"\b(?:roles?|jobs?|positions?|postings?|openings?)\b",
    re.I,
)
_DISCOVERY_OUTREACH_WORKFLOW_RE = re.compile(
    r"\b(?:find|identify|search|scout|source|discover|list)\b[\s\S]*?"
    r"\b(?:outreach|emails?|messages?|companies|targets?|leads?|opportunities?)\b[\s\S]*?"
    r"\b(?:draft|write|compose|prepare|send|outreach|emails?|messages?|companies|targets?|leads?)\b",
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
    normalized_agent = normalize_manual_agent(requested_agent) or _direct_agent_prefix_agent(text)
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
        task_objective=_task_objective(text, target_agent=target_agent, intent=intent),
        expected_artifact_type=_expected_artifact_type(
            text,
            target_agent=target_agent,
            intent=intent,
        ),
        desired_count=desired_count,
        constraints=_constraints(text),
        required_entities=_required_entities(text),
        required_terms=_required_terms(text),
        gmail_query=_gmail_query(text) if target_agent == "gmail_triage" else "",
        lookback_days=_lookback_days(text) if target_agent == "gmail_triage" else None,
        draft_policy=_draft_policy(text) if target_agent == "gmail_triage" else "",
        recipient=_recipient(text) if target_agent == "outreach_composer" else "",
        outreach_channel=_outreach_channel(text) if target_agent == "outreach_composer" else "",
        tone=_tone(text) if target_agent == "outreach_composer" else "",
        requires_live_search=_requires_live_search_for_plan(text, target_agent=target_agent),
        requires_approved_context=target_agent == "outreach_composer",
        side_effect_policy="draft_or_read_only",
        rationale="Local semantic planner inferred the manual request before agent execution.",
    )
    if target_agent == "clarification":
        plan.planner_warnings.append(
            "Manual request did not contain enough information for a safe route."
        )
    if _looks_like_blocked_side_effect_request(text):
        plan.planner_warnings.append(
            "Send request blocked; external send/write requests remain draft/read-only."
        )
    return plan


def _requires_live_search_for_plan(text: str, *, target_agent: ManualTargetAgent) -> bool:
    if target_agent not in {"business_research_analyst", "opportunity_scout"}:
        return False
    return not bool(_NO_EXTERNAL_RESEARCH_RE.search(str(text or "")))


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
    explicit_agent = base.requested_agent not in {None, "", "orchestrator"}
    if explicit_agent and plan.target_agent != base.target_agent:
        warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
        warnings.append("Ignored planner override that rerouted an explicit named-agent request.")
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
    if not merged.required_entities:
        merged.required_entities = list(base.required_entities)
    if not merged.required_terms:
        merged.required_terms = list(base.required_terms)
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
    explicit_text_agent = _direct_agent_prefix_agent(text)
    conversational_agent = _conversational_named_agent(text)
    if requested_agent in {None, "orchestrator"} and explicit_text_agent not in {
        None,
        "orchestrator",
    }:
        return explicit_text_agent
    if requested_agent in {None, "orchestrator"} and conversational_agent not in {
        None,
        "orchestrator",
    }:
        return conversational_agent
    if _looks_like_browser_diagnostics_only_request(text):
        return (
            requested_agent
            if requested_agent in {"chief_of_staff", "orchestrator"}
            else "chief_of_staff"
        )
    if requested_agent == "business_research_analyst" and _looks_like_unnamed_company_set_discovery(
        text
    ):
        return "opportunity_scout"
    if requested_agent and requested_agent != "orchestrator":
        return requested_agent
    if _looks_like_eval_scorecard_review(lower):
        return "chief_of_staff"
    if _looks_like_research_table_synthesis(text):
        return "business_research_analyst"
    if _looks_like_chief_of_staff_operational_request(lower):
        return "chief_of_staff"
    if _looks_like_gmail_label_request(lower):
        return "gmail_triage"
    if _looks_like_outreach_variant_request(lower):
        return "outreach_composer"
    if looks_like_opportunity_to_outreach_loop(text):
        return "opportunity_scout"
    if (
        looks_like_send_side_effect(text)
        and _looks_like_direct_outreach_send_request(lower)
        and not _looks_like_discovery_outreach_workflow(text)
    ):
        return "outreach_composer"
    if looks_like_send_side_effect(text) and not _looks_like_discovery_outreach_workflow(text):
        return "clarification"
    if _company_comparison_target(text):
        return "business_research_analyst"
    if looks_like_resume_request(text):
        return "orchestrator"
    if looks_like_zotero_article_request(text) or looks_like_zotero_collection_request(text):
        return "business_research_analyst"
    if _looks_like_orchestrator_owned_workflow(text):
        return _workflow_start_agent(text)
    if _looks_like_gmail_followup_request(lower):
        return "gmail_triage"
    if looks_like_company(text) and re.search(
        r"\b(?:research\s+brief|company\s+research|company\s+profile|"
        r"source-attributed|source\s+attributed|confirmed\s+facts)\b",
        lower,
    ):
        return "business_research_analyst"
    if looks_like_email(request, text):
        return "gmail_triage"
    if OUTREACH_RE.search(lower):
        if _looks_like_discovery_outreach_workflow(text):
            return "opportunity_scout"
        return "outreach_composer"
    if OPPORTUNITY_RE.search(lower):
        return "opportunity_scout"
    if _looks_like_discovery_outreach_workflow(text):
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
    if _looks_like_blocked_side_effect_request(
        text
    ) and not _looks_like_discovery_outreach_workflow(text):
        return "blocked_send"
    if target_agent in _CONTEXT_AGENT_TARGETS:
        return "context_lookup"
    if target_agent == "business_research_analyst" and _company_comparison_target(text):
        return "company_research"
    if target_agent == "business_research_analyst" and _looks_like_research_table_synthesis(text):
        return "company_research"
    if looks_like_resume_request(text):
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
                "thread",
                "selected message",
                "selected slack",
                "operator request",
                "unresolved",
                "follow-up",
                "follow up",
                "summarize",
            )
        )
    )


def _looks_like_chief_of_staff_operational_request(lower: str) -> bool:
    if _looks_like_eval_scorecard_review(lower):
        return True
    if _looks_like_slack_operations_request(lower):
        return True
    action_markers = (
        "audit",
        "review",
        "inspect",
        "diagnose",
        "debug",
        "evaluate",
        "assess",
        "recommend",
        "propose",
        "identify",
        "summarize",
        "list",
        "check",
    )
    if not any(marker in lower for marker in action_markers):
        return False
    operational_markers = (
        "business-agent architecture",
        "business agent architecture",
        "agent architecture",
        "architecture changes",
        "orchestrator",
        "planner",
        "manager loop",
        "implementation step",
        "implementation steps",
        "automation",
        "automations",
        "workitem",
        "work item",
        "bridge",
        "slack thread",
        "selected slack",
        "runtime state",
        "operator request",
        "operator requests",
        "previous @kni response",
        "@kni response",
        "unrelated response",
        "wrong response",
        "agent path",
        "backlog",
        "finance operations",
        "finance operations context",
    )
    return any(marker in lower for marker in operational_markers)


def _looks_like_eval_scorecard_review(lower: str) -> bool:
    if "coordinate the agents" in lower and "company research brief" in lower:
        return False
    return bool(
        re.search(
            r"\b(?:scorecard|score\s+the\s+workflow|human\s+reviewer|"
            r"here\s+are\s+my\s+scores|my\s+scores)\b",
            lower,
        )
        and re.search(r"\b(?:eval|promptfoo|case|workflow|scores?|missing\s+evidence)\b", lower)
    )


def _looks_like_research_table_synthesis(text: str) -> bool:
    lower = str(text or "").lower()
    if not re.search(r"\b(?:table|comparison|compare|matrix)\b", lower):
        return False
    if not re.search(r"\b(?:vendor|vendors|company|companies|platform|platforms)\b", lower):
        return False
    return bool(
        re.search(r"\bsource[- ]backed\b", lower)
        or re.search(r"\bsource[- ]provided\b", lower)
        or re.search(r"\bvendor\s+(?:summaries|summary|a|b)\b", lower)
        or re.search(r"\b(?:buyer|evidence|risk|next\s+safe\s+action)\b", lower)
    )


def _looks_like_gmail_label_request(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(?:label|tag|mark)\b[^.\n]{0,120}"
            r"\b(?:selected\s+messages?|messages?|emails?|gmail|thread)\b",
            lower,
        )
        or re.search(
            r"\b(?:selected\s+messages?|messages?|emails?|gmail|thread)\b[^.\n]{0,120}"
            r"\b(?:label|tag|mark)\b",
            lower,
        )
    )


def _looks_like_gmail_followup_request(lower: str) -> bool:
    return bool(
        re.search(r"\b(?:email|gmail|inbox|messages?)\b", lower)
        and re.search(r"\b(?:follow\s+up|follow-up|reply|replies|draft\s+replies)\b", lower)
    )


def _looks_like_outreach_variant_request(lower: str) -> bool:
    return bool(
        re.search(r"\b(?:create|prepare|draft|write|compose)\b", lower)
        and (
            re.search(r"\b(?:linkedin|email|outreach|message|note)\s+variant\b", lower)
            or re.search(r"\boutreach\s+versions?\b", lower)
        )
    )


def _looks_like_direct_outreach_send_request(lower: str) -> bool:
    if not re.search(r"\b(?:send|publish|post|share)\b", lower):
        return False
    if re.search(
        r"\b(?:outreach|email|linkedin|message|note|draft|version|variant|reply|response)\b",
        lower,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:ceo|founder|co[- ]?founder|president|director|head|vp|chief|"
            r"partner|partnerships?|buyer|recipient)\b",
            lower,
        )
    )


def _looks_like_blocked_side_effect_request(text: str) -> bool:
    lower = " ".join(str(text or "").lower().split())
    if (
        re.search(r"\bdraft\b[\s\S]{0,120}\b(?:response|reply|email|message|outreach)\b", lower)
        and re.search(r"\b(?:after|with|pending)\s+(?:human\s+)?approval\b", lower)
        and not re.search(r"\b(?:send|post|publish|share|deliver)\s+(?:it|the|this)?\b", lower)
    ):
        return False
    return looks_like_send_side_effect(text) or _looks_like_external_write_side_effect(text)


def _looks_like_external_write_side_effect(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    lower = re.sub(r"\bprior\s+post\b", "prior message", lower)
    if re.search(
        r"\b(?:do\s+not|don't|dont|never|no)\b[\s\S]{0,180}"
        r"\b(?:create|update|delete|modify|write|save|attach|export|move|share|schedule|publish|post|send|deliver)\b",
        lower,
    ):
        return False
    write_verb = (
        r"(?:create|update|delete|modify|write|save|attach|export|move|share|schedule|publish|post|send|deliver)"
    )
    target_object = (
        r"(?:record|row|table|tracker|field|file|doc|document|sheet|folder|attachment|"
        r"airtable|drive|workspace|gmail\s+draft|slack|crm|calendar|collection|item|library)"
    )
    return bool(
        re.search(rf"\b{write_verb}\b[\s\S]{{0,100}}\b{target_object}\b", lower)
        or re.search(rf"\b{target_object}\b[\s\S]{{0,100}}\b{write_verb}\b", lower)
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
    if match is not None:
        raw = match.group("count") or match.group("count2")
        try:
            return max(1, min(10, int(raw)))
        except (TypeError, ValueError):
            return 1
    word_match = _COUNT_WORD_RE.search(text)
    if word_match is None:
        return 1
    raw_word = (word_match.group("count_word") or word_match.group("count_word2") or "").lower()
    return _COUNT_WORDS.get(raw_word, 1)


def _primary_target(text: str, *, target_agent: ManualTargetAgent) -> str:
    cleaned = _strip_direct_agent_prefix(text).strip()
    if _looks_like_browser_diagnostics_request(cleaned):
        url_match = _REFERENCE_URL_RE.search(cleaned)
        return url_match.group(0).rstrip(".,;") if url_match else cleaned[:120]
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(cleaned.lower()):
        return _reference_target(cleaned)
    if target_agent == "business_research_analyst":
        comparison_target = _company_comparison_target(cleaned)
        if comparison_target:
            return comparison_target
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
        workflow_target = _workflow_company_target(cleaned)
        if workflow_target:
            return workflow_target
    if target_agent == "opportunity_scout":
        if looks_like_opportunity_to_outreach_loop(cleaned):
            return _opportunity_to_outreach_topic(cleaned)
        if _looks_like_orchestrator_owned_workflow(cleaned) and re.search(
            r"\b(?:best\s+company|best\s+candidate|find\s+companies|"
            r"business\s+development\s+opportunities)\b",
            cleaned,
            flags=re.I,
        ):
            return "behavioral health AI clinical research"
        workflow_target = _workflow_company_target(cleaned)
        if workflow_target:
            return workflow_target
        return _first_nonempty(
            _opportunity_search_target(cleaned), _quoted_text(cleaned), cleaned[:120]
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
    cleaned = _strip_opportunity_output_tail(cleaned)
    cleaned = re.sub(
        r"^\s*(?:please\s+)?(?:compare|discover|find|identify|source|search\s+for|look\s+for|list|return|show)\s+",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"^\s*(?:top\s+)?(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\b(?:opportunit(?:y|ies)|leads|targets)\b\s*$",
        "opportunities",
        cleaned,
        flags=re.I,
    )
    cleaned = " ".join(cleaned.split()).strip(" .,:;-")
    return cleaned[:160]


def _strip_opportunity_output_tail(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.split(
        r"\s+(?:please\s+)?(?:give|provide|return|show|include)\s+(?:me\s+)?"
        r"(?:a\s+|an\s+|the\s+)?(?:compact|short|small|brief|readable|ranked|formatted|"
        r"visible|source|metadata|synthesis|comparison|table)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]
    cleaned = re.sub(
        r"\s*(?:,?\s*(?:and|with|plus)\s+"
        r"(?:recommend|include|list|provide|return|show|summari[sz]e)\b"
        r"[\s\S]{0,120}\bnext[- ]steps?\b[\s\S]*)$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\s*(?:,?\s*(?:and|with|plus)\s+(?:a\s+)?(?:final\s+)?"
        r"next[- ]step(?:\s+recommendation)?\b[\s\S]*)$",
        "",
        cleaned,
        flags=re.I,
    )
    return cleaned


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
        if _contains_word_or_phrase(lower, ("institute", "center", "program", "lab")):
            return "institute"
        if any(marker in lower for marker in ("conference", "meeting", "symposium", "summit")):
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
        if _looks_like_unnamed_company_set_discovery(text):
            return "topic"
        if _ROLE_DISCOVERY_RE.search(text):
            return "opportunity"
        if any(
            marker in lower
            for marker in ("conference", "meeting", "meetings", "symposium", "summit")
        ):
            return "conference"
        if any(marker in lower for marker in ("researcher", "principal investigator", "faculty")):
            return "person"
        if _contains_word_or_phrase(lower, ("institute", "center", "program", "lab")):
            return "institute"
        if any(marker in lower for marker in ("company", "companies", "startup", "vendor")):
            return "company"
        return "topic"
    return _ROUTE_TARGET_TYPE.get(target_agent, "unknown")


def _contains_word_or_phrase(lower_text: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(marker)}\b", lower_text) for marker in markers)


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


def _task_objective(
    text: str,
    *,
    target_agent: ManualTargetAgent,
    intent: ManualRequestIntent,
) -> ManualTaskObjective:
    lower = _strip_direct_agent_prefix(text).lower()
    if intent == "blocked_send":
        return "blocked_side_effect"
    if intent == "continue_work_item":
        return "route_or_continue"
    if intent == "browser_diagnostics":
        return "browser_diagnostics"
    if intent == "reference_capture":
        return "reference_capture"
    if intent == "context_lookup":
        return "context_lookup"
    if intent == "gmail_triage":
        return "gmail_triage"
    if intent == "outreach_draft":
        return "outreach_draft"
    if intent == "slack_operations":
        return "slack_operations"
    if intent == "opportunity_to_outreach_loop":
        return "opportunity_discovery"
    if intent == "company_research":
        return (
            "source_research"
            if (
                _looks_like_source_summary_request(lower)
                or _looks_like_research_table_synthesis(lower)
                or target_agent == "business_research_analyst"
            )
            else "entity_research"
        )
    if intent == "research_brief":
        return "source_research"
    if intent == "opportunity_search":
        if _looks_like_source_summary_request(
            lower
        ) and not _looks_like_actionable_opportunity_request(lower):
            return "source_research"
        return "opportunity_discovery"
    if target_agent == "clarification":
        return "clarification"
    return "route_or_continue"


def _expected_artifact_type(
    text: str,
    *,
    target_agent: ManualTargetAgent,
    intent: ManualRequestIntent,
) -> ManualExpectedArtifactType:
    objective = _task_objective(text, target_agent=target_agent, intent=intent)
    if objective == "source_research":
        lower = _strip_direct_agent_prefix(text).lower()
        if _looks_like_source_summary_request(lower) or _looks_like_research_table_synthesis(lower):
            return "source_summary"
        if (
            intent == "company_research"
            and target_agent == "business_research_analyst"
            and _target_type(text, target_agent=target_agent) == "company"
        ):
            return "research_brief"
        return "source_summary"
    if objective == "entity_research":
        return "research_brief"
    if objective == "opportunity_discovery":
        return "opportunity_record"
    if objective == "contact_discovery":
        return "contact_candidates"
    if objective == "outreach_draft":
        return "outreach_draft"
    if objective == "gmail_triage":
        return "gmail_triage_report"
    if objective == "slack_operations":
        return "slack_ops_summary"
    if objective == "browser_diagnostics":
        return "browser_diagnostics_report"
    if objective == "reference_capture":
        return "reference_note"
    if objective == "context_lookup":
        return "context_summary"
    return "none"


def _looks_like_source_summary_request(lower: str) -> bool:
    return any(
        marker in lower
        for marker in (
            "summary",
            "summaries",
            "summarize",
            "recap",
            "recaps",
            "highlight",
            "highlights",
            "takeaway",
            "takeaways",
            "analysis",
            "analyze",
            "review",
            "brief",
            "what happened",
        )
    )


def _looks_like_actionable_opportunity_request(lower: str) -> bool:
    return any(
        marker in lower
        for marker in (
            "opportunity",
            "opportunities",
            "role",
            "roles",
            "job",
            "jobs",
            "position",
            "positions",
            "posting",
            "postings",
            "opening",
            "openings",
            "call for abstracts",
            "call for speakers",
            "speaker",
            "speaking",
            "presentation",
            "poster",
            "sponsor",
            "exhibitor",
            "submit",
            "submission",
            "deadline",
            "contact",
            "outreach",
            "apply",
            "proposal",
        )
    )


def _required_entities(text: str) -> list[str]:
    entities: list[str] = []
    cleaned = _strip_direct_agent_prefix(text)
    if re.search(r"\bAPA\b", cleaned, flags=re.I):
        entities.append("APA")
        if re.search(r"\bpsychiatr", cleaned, flags=re.I):
            entities.append("American Psychiatric Association")
    quoted = _quoted_text(cleaned)
    if quoted:
        entities.append(quoted)
    return list(dict.fromkeys(item for item in entities if item))


def _required_terms(text: str) -> list[str]:
    terms: list[str] = []
    cleaned = _strip_direct_agent_prefix(text)
    for match in re.finditer(r"\b20\d{2}\b", cleaned):
        terms.append(match.group(0))
    for phrase in ("San Francisco", "Philadelphia", "Pennsylvania", "Q1", "Q2", "Q3", "Q4"):
        if re.search(rf"\b{re.escape(phrase)}\b", cleaned, flags=re.I):
            terms.append(phrase)
    if re.search(r"\bAPA\b", cleaned, flags=re.I):
        terms.append("APA")
    return list(dict.fromkeys(item for item in terms if item))


def _looks_like_orchestrator_owned_workflow(text: str) -> bool:
    cleaned = str(text or "")
    lower = cleaned.lower()
    if "outreach" not in lower and "email" not in lower:
        return False
    if looks_like_email(None, cleaned) and not re.search(
        r"\b(?:coordinate|sequence|which\s+agents|agents?\s+should|routing\s+plan|"
        r"run\s+(?:the\s+)?workflow|multi[- ]agent|safe\s+parts\s+first|"
        r"score\s+the\s+workflow)\b",
        cleaned,
        flags=re.I,
    ):
        return False
    if not _WORKFLOW_AGENT_MARKER_RE.search(cleaned):
        return False
    return bool(_WORKFLOW_RESEARCH_MARKER_RE.search(cleaned))


def _workflow_start_agent(text: str) -> ManualTargetAgent:
    lower = str(text or "").lower()
    if (
        "best company" in lower
        or "candidate company" in lower
        or "best candidate" in lower
        or "find companies" in lower
        or "business development opportunities" in lower
    ):
        return "opportunity_scout"
    if _workflow_company_target(text) or "company research" in lower or "research brief" in lower:
        return "business_research_analyst"
    if OPPORTUNITY_RE.search(lower):
        return "opportunity_scout"
    return "business_research_analyst"


def _workflow_company_target(text: str) -> str:
    cleaned = str(text or "")
    for pattern in (
        _TARGET_COMPANY_RE,
        _COMPANY_NAME_RE,
        _COMPANY_LABEL_RE,
        _RESEARCH_ON_COMPANY_RE,
        _RESEARCH_COMPANY_ACTION_RE,
        _COMPANY_WORTH_RE,
        _COMPANY_LOOK_RE,
        _FOR_COMPANY_RE,
    ):
        match = pattern.search(cleaned)
        if not match:
            continue
        candidate = _clean_company_candidate(match.group("name"))
        if candidate:
            return candidate
    return ""


def _company_comparison_target(text: str) -> str:
    match = _COMPANY_COMPARISON_RE.search(str(text or ""))
    if not match:
        return ""
    company_a = _clean_company_candidate(match.group("company_a"))
    company_b = _clean_company_candidate(match.group("company_b"))
    if not company_a or not company_b:
        return ""
    return f"{company_a} vs {company_b}"


def _looks_like_unnamed_company_set_discovery(text: str) -> bool:
    if _company_comparison_target(text):
        return False
    lower = str(text or "").lower()
    return bool(
        re.search(r"\b(?:compare|find|identify|discover|list|source)\b", lower)
        and re.search(r"\b(?:companies|vendors|platforms|tools)\b", lower)
        and (
            _desired_count(text) > 1
            or re.search(r"\b(?:three|four|five|several|multiple)\b", lower)
        )
    )


def _clean_company_candidate(value: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" .,:;-[]")
    cleaned = re.split(
        r"(?:[.;]|\bwebsite\s*:|\blinkedin\s+page\s*:|\btarget\s+persona\b|"
        r"\bcoordinate\b|\bexpected\s+outputs\b)",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .,:;-[]")
    cleaned = re.split(
        r"\b(?:to|and|or|if|for|with|as|possible|potential|relevant|worth|from|using|about)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .,:;-[]")
    if cleaned.lower() in {"keystone", "keystone's", "help", "me"}:
        return ""
    return cleaned[:120]


def _constraints(text: str) -> list[str]:
    lower = text.lower()
    constraints: list[str] = []
    for marker in (
        "u.s.",
        "current",
        "recent",
        "behavioral health",
        "digital mental health",
        "psychiatry",
        "clinical ai",
        "clinical research",
        "neuroinformatics",
        "evidence-generation",
        "implementation",
        "conference",
        "research collaboration",
        "advisory",
        "remote",
        "active",
    ):
        if marker in lower:
            constraints.append(marker)
    if re.search(r"\b(?:us|u\.s\.|united\s+states)\b", lower):
        constraints.append("us-relevant")
    if re.search(r"\blast\s+\d+\s+days?\b", lower):
        constraints.append("recent")
    if re.search(r"\b(?:deep|deeper|deepened|detailed)\s+(?:web\s+)?search\b", lower):
        constraints.append("deeper-search")
    if re.search(r"\bsource[- ]backed\b", lower):
        constraints.append("source-backed")
    if re.search(r"\bvisible\s+source\s+urls?\b|\bsource\s+urls?\b", lower):
        constraints.append("visible-source-urls")
    if re.search(r"\bprovider\s+(?:diagnostics|comparison|usage|metadata)\b", lower):
        constraints.append("provider-diagnostics")
    if re.search(r"\b(?:metadata|providers?\s+used|lane\s+status)\b", lower):
        constraints.append("metadata-section")
    if re.search(r"\b(?:comparison|compare|table|matrix)\b", lower):
        constraints.append("comparison-format")
    if re.search(r"\banswer\b", lower) and re.search(r"\bsynthesis\b", lower):
        constraints.append("answer-and-synthesis")
    constraints.extend(_exclusion_constraints(text))
    return constraints


def _exclusion_constraints(text: str) -> list[str]:
    constraints: list[str] = []
    for match in re.finditer(
        r"\bexclude\s+(?P<constraint>.*?)(?=(?:,\s*(?:and\s+)?exclude\b|\s+and\s+exclude\b|;\s*exclude\b|\.|$))",
        str(text or ""),
        flags=re.I,
    ):
        cleaned = " ".join(match.group("constraint").split()).strip(" ,.;:-")
        if cleaned:
            constraints.append(f"exclude {cleaned}")
    return list(dict.fromkeys(constraints))


def looks_like_opportunity_to_outreach_loop(text: str) -> bool:
    """Return whether text asks for the integrated opportunity -> outreach workflow."""

    raw_text = str(text or "")
    if _NO_OUTREACH_DRAFT_RE.search(raw_text):
        return False
    return bool(_OPPORTUNITY_TO_OUTREACH_RE.search(raw_text))


def _looks_like_discovery_outreach_workflow(text: str) -> bool:
    return bool(_DISCOVERY_OUTREACH_WORKFLOW_RE.search(str(text or "")))


def _strip_direct_agent_prefix(text: str) -> str:
    cleaned = _strip_kni_direct_prefix(text)
    lowered = cleaned.lower()
    for alias in sorted(_AGENT_ALIASES, key=len, reverse=True):
        if lowered.startswith(alias + " "):
            return _strip_wrapping_quotes(cleaned[len(alias) :].strip())
    return _strip_wrapping_quotes(cleaned)


def _strip_wrapping_quotes(text: str) -> str:
    cleaned = str(text or "").strip()
    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”")}
    for start, end in quote_pairs:
        if cleaned.startswith(start) and cleaned.endswith(end) and len(cleaned) >= 2:
            return cleaned[1:-1].strip()
    return cleaned


def _direct_agent_prefix_agent(text: str) -> ManualTargetAgent | None:
    cleaned = _strip_kni_direct_prefix(text).lower()
    for alias in sorted(_AGENT_ALIASES, key=len, reverse=True):
        if cleaned == alias or re.match(rf"{re.escape(alias)}(?:\s|[:;,.-])", cleaned):
            return _AGENT_ALIASES[alias]
    return None


def _conversational_named_agent(text: str) -> ManualTargetAgent | None:
    """Detect polite natural-language asks that name a specific agent."""

    cleaned = _strip_kni_direct_prefix(text).lower()
    lead = cleaned[:160]
    for alias in sorted(_AGENT_ALIASES, key=len, reverse=True):
        if alias == "analyst":
            continue
        if re.search(
            rf"\b(?:could|can|would|please|ask|have|get|let)\b"
            rf".{{0,80}}\b(?:the\s+)?{re.escape(alias)}\b",
            lead,
        ):
            return _AGENT_ALIASES[alias]
    return None


def _strip_kni_direct_prefix(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    for _ in range(2):
        lowered = cleaned.lower()
        for prefix in ("@kni", "kni", "business agents", "business agent"):
            if lowered == prefix:
                return ""
            if lowered.startswith(prefix + " "):
                cleaned = cleaned[len(prefix) :].strip(" :")
                break
        else:
            break
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
    without_url = _strip_research_output_format_tail(without_url)
    without_url = _strip_source_bundle_constraint_tail(without_url)
    parts = re.split(r"\b(?:for|about|where|with|using|to)\b", without_url, maxsplit=1, flags=re.I)
    candidate = parts[0].strip(" .:,-")
    capitalized = re.match(
        r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b",
        candidate,
    )
    if capitalized:
        return capitalized.group("name").strip(" .:,-")
    candidate = re.sub(
        r"\b(?:recent|current|source-backed|concise|brief|company)\b", "", candidate, flags=re.I
    )
    candidate = " ".join(candidate.split()).strip(" :,-")
    if 1 <= len(candidate.split()) <= 6:
        return candidate
    return ""


def _strip_research_output_format_tail(text: str) -> str:
    return re.split(
        r"\b(?:return\s+exactly|return\s+as|use\s+sections|include\s+sections|"
        r"format\s+as|sections\s*:)\b",
        str(text or ""),
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .:,-")


def _strip_source_bundle_constraint_tail(text: str) -> str:
    return re.split(
        r"\b(?:from|using|with)\s+(?:the\s+)?(?:provided\s+)?"
        r"(?:source\s+bundle|sources?|source\s+ids?)\s+only\b",
        str(text or ""),
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .:,-")


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
