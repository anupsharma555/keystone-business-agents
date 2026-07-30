"""Pre-execution planning for manual Keystone agent calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from keystone_agents.calendar_actions import is_calendar_action_candidate
from keystone_agents.gmail_triage.relationship_query import (
    extract_known_contact_entity,
    looks_like_known_contact_relationship,
)
from keystone_agents.local_kni_evidence import looks_like_local_kni_evidence_lookup
from keystone_agents.orchestrator.routing import (
    OPPORTUNITY_RE,
    OUTREACH_RE,
    looks_like_company,
    looks_like_email,
    looks_like_gmail_collection_read,
    looks_like_resume_request,
    looks_like_send_side_effect,
    looks_like_thread_local_draft_request,
    normalize_match_text,
    payload_text,
)
from keystone_agents.schemas.manual_request_plan import (
    AskShapePolicy,
    ManualExpectedArtifactType,
    ManualProviderActionStep,
    ManualProviderResultSetScope,
    ManualProviderSystem,
    ManualRequestIntent,
    ManualRequestPlan,
    ManualTargetAgent,
    ManualTargetType,
    ManualTaskObjective,
)
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints
from keystone_agents.semantic_execution import ExecutionIntentAuthority
from keystone_agents.zotero_research import (
    extract_zotero_article_query,
    extract_zotero_collection_hint,
    looks_like_zotero_article_request,
    looks_like_zotero_collection_request,
)

_AGENT_ALIASES: dict[str, ManualTargetAgent] = {
    "orchestrator": "orchestrator",
    "orchestrator agent": "orchestrator",
    "business research": "business_research_analyst",
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
    "cos": "chief_of_staff",
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
    "google slides context": "google_workspace_context_agent",
    "powerpoint context": "google_workspace_context_agent",
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
    "chief_of_staff": "route_request",
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
    "chief_of_staff": "unknown",
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
_MUTABLE_CONTEXT_AGENT_TARGETS: frozenset[ManualTargetAgent] = frozenset(
    {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    }
)
_DIRECT_RESPONSE_TARGETS: frozenset[ManualTargetAgent] = frozenset(
    {
        "chief_of_staff",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "gmail_triage",
    }
)
_FINANCE_EXPENSE_RECEIPT_WRITE_RE = re.compile(
    r"\bairtable\b[\s\S]{0,240}\b(?:business|personal)\s+expenses?\b"
    r"|\b(?:business|personal)\s+expenses?\b[\s\S]{0,240}\bairtable\b",
    re.I,
)
_LOCAL_ARTIFACT_PATH_RE = re.compile(
    r"(?:~|/Users/|/private/|/tmp/)[^\s\"'<>]+?\.(?:pdf|png|jpe?g|webp|gif)",
    re.I,
)
_LOCAL_ATTACHMENT_CONTEXT_RE = re.compile(
    r"\b(?:attachment|attached|diagram|image|pdf|receipt|screenshot|file|"
    r"document|photo)\b",
    re.I,
)
_SLACK_ATTACHMENT_PATH_MARKER = "operator-supplied slack attachment local path:"
_COUNT_RE = re.compile(
    r"\b(?:compare|discover|find|return|list|top|show|identify|source)\s+"
    r"(?:how\s+)?"
    r"(?:up\s+to\s+)?(?P<count>\d{1,2})\b"
    r"|\b(?P<count2>\d{1,2})\s+"
    r"(?:[a-z][\w-]*\s+){0,4}"
    r"(?:opportunities|companies|institutes|researchers|conferences|people|leads|emails|"
    r"roles|jobs|positions|postings|openings|products|targets|vendors|"
    r"preprints|papers|articles|bullets|items|points|talking\s+points|"
    r"recommendations)\b",
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
    r"(?:how\s+)?"
    r"(?:up\s+to\s+|the\s+)?"
    r"(?P<count_word>one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b(?P<count_word2>one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:[a-z][\w-]*\s+){0,4}"
    r"(?:opportunities|companies|institutes|researchers|conferences|people|leads|emails|"
    r"roles|jobs|positions|postings|openings|products|targets|vendors|"
    r"preprints|papers|articles|bullets|items|points|talking\s+points|"
    r"recommendations)\b",
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
    r"\bopportunit(?:y|ies)\b.*\b(?:draft|compose|prepare)\b.*\boutreach\b"
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
_NEGATED_ROUTE_ACTION_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:scout|find|identify|search|source|discover|list|"
    r"assess|evaluate|review|qualify)\b"
    r"[^.;\n]{0,160}\b(?:opportunities?|leads?|grants?|partners?|"
    r"partnerships?|pilots?|companies?|targets?|roles?|jobs?|positions?|"
    r"postings?|openings?)\b"
    r"[^.;\n]*[.;]?"
    r"|"
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:draft|drafting|compose|write|prepare|outline|"
    r"create|queue|produce)\b"
    r"[^.;\n]{0,160}\b(?:outreach|emails?|messages?|reply|response|"
    r"follow-up|followup|note)\b"
    r"[^.;\n]*[.;]?"
    r"|"
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:outreach|emails?|messages?|reply|response|"
    r"follow-up|followup|note)\b"
    r"[^.;\n]{0,160}\b(?:draft|drafting|compose|write|prepare|outline|"
    r"create|queue|produce)\b"
    r"[^.;\n]*[.;]?",
    re.I,
)
_NEGATED_BUSINESS_SYSTEM_ACTION_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:read|list|find|search|query|summari[sz]e|inspect|review|"
    r"create|add|append|update|modify|edit|write|delete|remove|attach|import)\b"
    r"[^.;\n]{0,160}\b(?:airtable|zotero|google\s+workspace|google\s+drive|"
    r"google\s+docs?|google\s+sheets?|drive|sheets?|rss|preprints?)\b"
    r"[^.;\n]*[.;]?"
    r"|"
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:create|add|append|update|modify|edit|write|delete|"
    r"remove|attach|import)\b"
    r"[^.;\n]{0,160}\b(?:provider\s+records?|external\s+records?)\b"
    r"[^.;\n]*[.;]?",
    re.I,
)
_LEADING_NEGATED_CAPABILITY_PREFIX_RE = re.compile(
    r"\b(?:no|without)\b"
    r"(?:(?!\b(?:but|however|instead)\b)[^,.;\n]){0,160},\s*"
    r"(?=(?:just\s+|only\s+|please\s+)?"
    r"(?:answer|use|using|based|draft|compose|write|prepare|return|give|provide|"
    r"summari[sz]e|review|assess|explain|reformat|list|read|query|find|"
    r"create|add|schedule|verify|check|confirm|update|modify|edit|delete|remove)\b)",
    re.I,
)
_NEGATED_CAPABILITY_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"(?:(?!\b(?:but|however|instead)\b)[^.;\n])*"
    r"(?=\b(?:but|however|instead)\b|[.;\n]|$)",
    re.I,
)
_CONDITIONAL_AGENT_REPORTING_CLAUSE_RE = re.compile(
    r"\b(?:if|when)\s+(?:recommending|mentioning|naming|listing|referring\s+to)\b"
    r"[^.;\n]{0,260}\b(?:agent|owner|route|specialist)\b"
    r"[^.;\n]{0,260}\b(?:notation|format|wording|label|name)\b"
    r"(?:(?!\bthen\b)[^.;\n])*?(?:,\s*)?"
    r"(?=\bthen\b|[.;\n]|$)",
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
    r"(?:(?:search|browse)(?:\s+(?:the\s+)?(?:web|internet))?|"
    r"web\s+search|live\s+web|live\s+search|external\s+(?:search|research|tools?)|"
    r"browser\s+automation|research\s+externally)\b"
    r"|"
    r"\b(?:(?:search|browse)\s+(?:the\s+)?(?:web|internet)|"
    r"web\s+search|live\s+web\s+search|live\s+search|external\s+"
    r"(?:search|research|tools?))\b[^.;\n]{0,80}\b"
    r"(?:not\s+approved|not\s+allowed|not\s+permitted|disabled|off-limits)\b"
    r"|"
    r"\b(?:use|using|based)\s+only(?:\s+on)?\b"
    r"[^.;\n]{0,160}\b"
    r"(?:context|note|packet|materials?|facts?|(?:selected\s+)?thread|email)\b",
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
_COMPANY_PROFILE_TARGET_PATTERNS = (
    re.compile(
        r"\bwho\s+is\s+(?P<name>.+?)"
        r"(?=\s+(?:and|then)\s+(?:summarize|describe|explain|profile)\b|[?.,;]|$)",
        re.I,
    ),
    re.compile(
        r"\bwhat\s+does\s+(?P<name>.+?)\s+do\b",
        re.I,
    ),
    re.compile(
        r"\btell\s+me\s+about\s+(?P<name>.+?)(?=[?.,;]|$)",
        re.I,
    ),
    re.compile(
        r"\b(?:summarize|describe|profile)\s+(?:the\s+company\s+)?(?P<name>.+?)"
        r"(?=\s+(?:in|within|using)\s+(?:(?:no\s+more\s+than|at\s+most|"
        r"under|exactly)\s+)?[1-9]\d{0,2}\s+words?\b|[?.,;]|$)",
        re.I,
    ),
)
_COMPANY_COMPARISON_RE = re.compile(
    r"\b(?:compare|comparison\s+of)\s+"
    r"(?P<company_a>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    r"\s+(?:and|vs\.?|versus)\s+"
    r"(?P<company_b>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b",
    re.I,
)
_COMPANY_LIST_COMPARISON_RE = re.compile(
    r"\bcompare\s+(?P<companies>[^.;:]+?)"
    r"(?=\s+and\s+(?:explain|summarize|tell|show|identify|describe)\b|[.;:]|$)",
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
_EXPLICIT_BUSINESS_RESEARCH_INSTRUCTION_RE = re.compile(
    r"\b(?:then\s+)?(?:run|use|call|route\s+to|handoff\s+to|hand\s+off\s+to)\s+"
    r"(?:the\s+)?business\s+research(?:\s+(?:analyst|agent))?\b"
    r"|"
    r"\bbusiness\s+research(?:\s+(?:analyst|agent))?\s+"
    r"(?:for|on|to\s+review|reviews?|as\s+an?\s+internal|as\s+the\s+next)\b",
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
    internal_contact_lookup = _looks_like_internal_gmail_contact_lookup_request(text)
    gmail_collection_read = looks_like_gmail_collection_read(text)
    gmail_thread_local_draft = bool(
        gmail_collection_read and looks_like_thread_local_draft_request(text)
    )
    desired_count = _desired_count(text)
    desired_count_explicit = _desired_count_is_domain_limit(text)
    target_agent = _semantic_target_agent(request, text, requested_agent=normalized_agent)
    chief_context_workflow, chief_work_items_requested = (
        _chief_multi_source_context_workflow(text)
        if normalized_agent == "chief_of_staff" or target_agent == "chief_of_staff"
        else ([], False)
    )
    chief_context_summary = bool(
        (normalized_agent == "chief_of_staff" or target_agent == "chief_of_staff")
        and (
            len(chief_context_workflow) >= 2
            or (chief_context_workflow and chief_work_items_requested)
        )
    )
    opportunity_workflow = _natural_opportunity_to_outreach_workflow(text)
    workflow = (
        chief_context_workflow
        if chief_context_summary
        else opportunity_workflow
        if opportunity_workflow
        else _infer_goal_workflow(text)
        if normalized_agent == "chief_of_staff" or target_agent == "chief_of_staff"
        else []
    )
    if gmail_thread_local_draft:
        target_agent = "gmail_triage"
        workflow = ["gmail_triage", "outreach_composer"]
    if workflow and normalized_agent == "chief_of_staff" and not gmail_thread_local_draft:
        desired_count = max(desired_count, len(workflow))
    if workflow and normalized_agent == "chief_of_staff" and not gmail_thread_local_draft:
        target_agent = "chief_of_staff"
    inferred_target_type = _target_type(text, target_agent=target_agent)
    inferred_primary_target = (
        "current operating context"
        if chief_context_summary
        else _opportunity_to_outreach_topic(text)
        if opportunity_workflow
        else _goal_workflow_primary_target(text)
        if workflow
        else _primary_target(text, target_agent=target_agent)
    )
    workflow_allowed = normalized_agent in {None, "orchestrator"}
    intent = (
        "outreach_draft"
        if gmail_thread_local_draft
        else "context_lookup"
        if chief_context_summary
        else "opportunity_to_outreach_loop"
        if opportunity_workflow
        else "route_request"
        if workflow
        else _intent_for_target(target_agent, text, workflow_allowed=workflow_allowed)
    )
    airtable_record_plan_requested = bool(
        opportunity_workflow and "airtable_context_agent" in opportunity_workflow
    )
    provider_system = (
        "airtable"
        if airtable_record_plan_requested
        else _provider_system_for_plan(
            text,
            target_agent=target_agent,
            intent=intent,
        )
    )
    zotero_context = bool(
        provider_system == "zotero" or target_agent == "zotero_context_agent"
    )
    zotero_selection_rank = _provider_selection_rank(
        text,
        zotero_context=zotero_context,
    )
    zotero_selection_order = _provider_selection_order(
        text,
        zotero_context=zotero_context,
    )
    zotero_fields = _zotero_requested_fields(
        text,
        zotero_context=zotero_context,
    )
    ask_shape = _ask_shape_policy(text)
    if chief_context_summary:
        source_by_route = {
            "gmail_triage": "gmail",
            "airtable_context_agent": "airtable",
            "google_workspace_context_agent": "google_workspace",
            "zotero_context_agent": "zotero",
            "rss_context_agent": "rss",
            "preprints_context_agent": "preprints",
        }
        required_context_sources = [
            source_by_route[route]
            for route in chief_context_workflow
            if route in source_by_route
        ]
        if chief_work_items_requested:
            required_context_sources.append("work_items")
        ask_shape = ask_shape.model_copy(
            update={
                "source_type_preference": list(
                    dict.fromkeys(
                        [
                            *ask_shape.source_type_preference,
                            *required_context_sources,
                        ]
                    )
                )
            }
        )
    if workflow and ask_shape.output_form == "draft" and not gmail_thread_local_draft:
        # In a coordinated review, the draft is one intermediate deliverable.
        # The final operator response should summarize the ordered owner results.
        ask_shape = ask_shape.model_copy(update={"output_form": "bullets"})
    plan = ManualRequestPlan(
        source=source,
        requested_agent=normalized_agent,
        target_agent=target_agent,
        workflow=workflow,
        intent=intent,
        primary_target=inferred_primary_target,
        target_type=(
            "business_system_context"
            if chief_context_summary
            else "zotero_article"
            if zotero_selection_rank is not None
            else "topic"
            if workflow and inferred_target_type == "unknown"
            else inferred_target_type
        ),
        provider_system=provider_system,
        provider_operations=(
            ["read"]
            if gmail_thread_local_draft
            or chief_context_summary
            or zotero_selection_rank is not None
            else []
            if airtable_record_plan_requested
            else _fallback_provider_operations(text, intent=intent)
        ),
        provider_read_scope=(
            "bounded_collection"
            if internal_contact_lookup
            or gmail_collection_read
            or zotero_selection_rank is not None
            else "unspecified"
        ),
        provider_result_mode=(
            "items"
            if internal_contact_lookup
            or gmail_collection_read
            or zotero_selection_rank is not None
            else "unspecified"
        ),
        provider_selection_order=zotero_selection_order,
        provider_selection_rank=zotero_selection_rank,
        gmail_mailbox_direction=(
            "any"
            if internal_contact_lookup
            else "inbound"
            if gmail_collection_read
            else "unspecified"
        ),
        gmail_date_scope=_explicit_gmail_date_scope(
            text,
            gmail_context=bool(
                gmail_collection_read
                or target_agent == "gmail_triage"
                or normalized_agent == "gmail_triage"
            ),
        ),
        gmail_exclude_threads_with_operator_reply=(
            _explicit_gmail_operator_reply_exclusion(
                text,
                gmail_context=bool(
                    gmail_collection_read
                    or target_agent == "gmail_triage"
                    or normalized_agent == "gmail_triage"
                    or "gmail_triage" in workflow
                ),
            )
        ),
        gmail_requested_fields=(
            ["sender", "subject", "date", "snippet"]
            if internal_contact_lookup
            else []
        ),
        zotero_requested_fields=zotero_fields,
        objective=_objective(text, intent=intent),
        task_objective=(
            "outreach_draft"
            if gmail_thread_local_draft
            else "context_lookup"
            if chief_context_summary
            else "opportunity_discovery"
            if opportunity_workflow
            else "route_or_continue"
            if workflow
            else _task_objective(text, target_agent=target_agent, intent=intent)
        ),
        expected_artifact_type=(
            "outreach_draft"
            if gmail_thread_local_draft
            else "context_summary"
            if chief_context_summary
            else "business_system_write_plan"
            if airtable_record_plan_requested
            else "none"
            if workflow
            else _expected_artifact_type(
                text,
                target_agent=target_agent,
                intent=intent,
            )
        ),
        desired_count=desired_count,
        desired_count_explicit=desired_count_explicit,
        constraints=_constraints(text),
        ask_shape=ask_shape,
        required_entities=_required_entities(text),
        required_terms=_required_terms(text),
        gmail_query=_gmail_query(text) if intent == "gmail_triage" else "",
        lookback_days=_lookback_days(text) if intent == "gmail_triage" else None,
        draft_policy=(
            _draft_policy(text)
            if intent == "gmail_triage" or gmail_thread_local_draft
            else ""
        ),
        recipient=_recipient(text) if intent == "outreach_draft" else "",
        outreach_channel=(
            "internal_slack"
            if gmail_thread_local_draft
            else _outreach_channel(text)
            if intent == "outreach_draft"
            else ""
        ),
        tone=_tone(text) if intent == "outreach_draft" else "",
        requires_live_search=_requires_live_search_for_plan(text, target_agent=target_agent),
        requires_approved_context=(
            False
            if gmail_thread_local_draft
            else intent in {"outreach_draft", "blocked_send"}
        ),
        requires_durable_state=bool(
            chief_work_items_requested
            or len(workflow) > 1
            or looks_like_stateful_work_request(text)
        ),
        side_effect_policy=(
            "internal_write_approval_required"
            if intent == "business_system_write"
            else "draft_or_read_only"
        ),
        rationale="Local semantic planner inferred the manual request before agent execution.",
    )
    if target_agent == "clarification":
        plan.planner_warnings.append(
            "Manual request did not contain enough information for a safe route."
        )
    if intent == "blocked_send" or looks_like_send_side_effect(text):
        plan.planner_warnings.append(
            "Send request blocked; external send/write requests remain draft/read-only."
        )
    return _apply_typed_provider_permission_ceiling(plan)


def _explicit_gmail_date_scope(
    text: str,
    *,
    gmail_context: bool,
) -> str:
    """Extract an exact mailbox-day override without granting Gmail authority."""

    if not gmail_context:
        return "unspecified"
    normalized = " ".join(str(text or "").lower().split())
    if re.search(r"\byesterday\b", normalized):
        return "yesterday"
    if re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", normalized):
        return "today"
    return "unspecified"


def _provider_selection_order(
    text: str,
    *,
    zotero_context: bool,
) -> str:
    """Extract explicit Zotero provider ordering for deterministic fallback."""

    if not zotero_context:
        return "unspecified"
    normalized = " ".join(str(text or "").lower().split())
    if _provider_selection_rank(text, zotero_context=True) is not None:
        return "latest"
    if re.search(
        r"\b(?:most\s+recent(?:ly)?|recently|newly|last)\s+added\b"
        r"|\bnewest(?:\s+added)?(?:\s+(?:zotero|library))?\s+(?:item|article|record)\b"
        r"|\b(?:latest|most\s+recent)\s+(?:item|article|record|paper)\b",
        normalized,
    ):
        return "latest"
    if re.search(
        r"\b(?:least\s+recently|earliest|first|oldest)\s+added\b",
        normalized,
    ):
        return "earliest"
    if re.search(r"\bprovider\s+order\b", normalized):
        return "provider_order"
    return "unspecified"


def _provider_selection_rank(
    text: str,
    *,
    zotero_context: bool,
) -> int | None:
    """Extract a bounded ordinal within the typed Zotero provider order."""

    if not zotero_context:
        return None
    normalized = " ".join(
        str(text or "").lower().replace("\u2011", "-").replace("\u2013", "-").split()
    )
    if re.search(
        r"\b(?:item|article|record|paper)\s+(?:immediately\s+)?before\s+"
        r"(?:the\s+)?latest\b",
        normalized,
    ):
        return 2
    ordinal_values = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
    }
    match = re.search(
        r"\b(?P<rank>first|second|third|fourth|fifth|sixth|seventh|eighth|"
        r"ninth|tenth|(?:[1-9]|10)(?:st|nd|rd|th))[- ]+"
        r"(?:most[- ]recent|newest|latest)\b",
        normalized,
    )
    if match is None:
        return None
    rank_text = match.group("rank")
    if rank_text in ordinal_values:
        return ordinal_values[rank_text]
    numeric = re.match(r"\d+", rank_text)
    return int(numeric.group()) if numeric is not None else None


def _zotero_requested_fields(
    text: str,
    *,
    zotero_context: bool,
) -> list[str]:
    """Project explicit Zotero output fields into the typed fallback contract."""

    if not zotero_context:
        return []
    normalized = " ".join(str(text or "").lower().split())
    fields: list[str] = []
    title_text = re.sub(r"\bpublication\s+title\b", "", normalized)
    if re.search(r"\btitle\b", title_text):
        fields.append("title")
    if re.search(r"\bauthors?\b|\bcreators?\b", normalized):
        fields.append("authors")
    if re.search(r"\bpublication\s+title\b|\bjournal\s+title\b", normalized):
        fields.append("publication_title")
    if re.search(r"\babstract\b", normalized):
        fields.append("abstract")
    if re.search(r"\bmetadata\b", normalized):
        fields.append("metadata")
    if re.search(r"\b(?:children|child\s+items?|attachments?|notes?)\b", normalized):
        fields.append("children")
    if re.search(r"\b(?:full\s*text|pdf\s+text|read\s+the\s+pdf)\b", normalized):
        fields.append("full_text")
    return fields


def _explicit_gmail_operator_reply_exclusion(
    text: str,
    *,
    gmail_context: bool,
) -> bool:
    """Type an explicit current-turn exclusion without granting Gmail authority."""

    if not gmail_context:
        return False
    normalized = " ".join(normalize_match_text(text).lower().split())
    return bool(
        re.search(
            r"\b(?:if\s+)?(?:i(?:'ve)?|the\s+operator|user)\s+(?:have\s+)?"
            r"(?:already\s+)?(?:repl(?:y|ied)|answer(?:ed)?)\b.{0,50}"
            r"\b(?:do\s+not|don't|dont|skip|exclude|avoid|not)\b"
            r"|"
            r"\b(?:do\s+not|don't|dont|skip|exclude|avoid)\b.{0,50}"
            r"\b(?:threads?|emails?|messages?|anything|anyone|any\s+one|"
            r"any\s+of\s+them|one|those|them|it)\b.{0,50}"
            r"\b(?:i(?:'ve)?|the\s+operator|user)\s+(?:have\s+)?(?:already\s+)?"
            r"(?:repl(?:y|ied)|answer(?:ed)?)\b(?:\s+to\b)?",
            normalized,
        )
    )


def _fallback_provider_operations(
    text: str,
    *,
    intent: ManualRequestIntent,
) -> list[str]:
    """Best-effort provider operations for offline or planner-unavailable mode.

    Live natural-language execution replaces this fallback with the structured
    operations interpreted by the LLM planner. These matches must never
    override an LLM plan or grant provider-write authority.
    """

    if intent not in {"business_system_write", "context_lookup", "gmail_triage"}:
        return []
    if intent == "gmail_triage" and _looks_like_internal_gmail_contact_lookup_request(text):
        return ["search", "read"]
    if intent == "gmail_triage" and looks_like_gmail_collection_read(text):
        return ["read"]
    positive_text = positive_capability_text(text).lower()
    patterns = {
        "read": r"\b(?:read|check|inspect|review|show|list|find|look\s+up)\b",
        "search": r"\b(?:search|query|locate)\b",
        "create": r"\b(?:add|create|insert|make|put|save|write)\b",
        "update": r"\b(?:change|edit|modify|revise|set|tighten|update)\b",
        "delete": r"\b(?:clean\s*up|delete|remove|throw\s+away|trash)\b",
        "attach": r"\b(?:attach|upload)\b",
        "verify": r"\b(?:check|confirm|read[- ]back|verify)\b",
    }
    matched = [
        (match.start(), operation)
        for operation, pattern in patterns.items()
        if (match := re.search(pattern, positive_text, re.I)) is not None
    ]
    return [operation for _, operation in sorted(matched)]


def _apply_typed_provider_permission_ceiling(
    plan: ManualRequestPlan,
) -> ManualRequestPlan:
    """Prevent typed read-only/output policy from granting provider mutations.

    Inline reply or draft text is a reader-facing artifact, not provider-create
    authority. This ceiling is deliberately provider-agnostic for read-only
    plans, with one Gmail-specific refinement for an explicit no-draft policy.
    Executors still enforce their own mutation boundary independently.
    """

    retained_operations = list(plan.provider_operations)
    if (
        plan.ask_shape.permission_state == "read_only"
        and plan.intent != "business_system_write"
    ):
        retained_operations = [
            operation
            for operation in retained_operations
            if operation in {"read", "search", "verify"}
        ]
    if plan.provider_system == "gmail" and plan.draft_policy == "no_drafts_requested":
        retained_operations = [
            operation
            for operation in retained_operations
            if operation not in {"create", "update"}
        ]
    if retained_operations == plan.provider_operations:
        return plan

    retained_set = set(retained_operations)
    return plan.model_copy(
        update={
            "provider_operations": retained_operations,
            "provider_action_steps": [
                step
                for step in plan.provider_action_steps
                if step.operation in retained_set
            ],
            "planner_warnings": list(
                dict.fromkeys(
                    [
                        *plan.planner_warnings,
                        "Removed provider mutations that exceeded the typed "
                        "read-only or no-draft permission boundary.",
                    ]
                )
            ),
        }
    )


def _provider_system_for_plan(
    text: str,
    *,
    target_agent: ManualTargetAgent,
    intent: ManualRequestIntent,
) -> ManualProviderSystem:
    """Record high-confidence provider ownership without granting tool authority."""

    if target_agent == "gmail_triage" and _request_forbids_provider_access(
        text,
        provider="gmail",
    ):
        # A named specialist can still answer a bounded transformation from
        # facts supplied in the current turn. Its organizational identity does
        # not authorize Gmail access when the operator explicitly forbids tools
        # or providers.
        return "unspecified"
    provider_by_owner = {
        "gmail_triage": "gmail",
        "airtable_context_agent": "airtable",
        "google_workspace_context_agent": "google_workspace",
        "zotero_context_agent": "zotero",
    }
    provider = provider_by_owner.get(target_agent)
    if provider:
        return provider
    positive_text = _without_negated_route_action_clauses(text)
    if re.search(r"\bgoogle\s+calendar\b", positive_text, re.I) or (
        target_agent == "chief_of_staff"
        and intent in {"business_system_write", "context_lookup"}
        and is_calendar_action_candidate(positive_text)
    ):
        return "google_calendar"
    if intent == "slack_operations":
        return "slack"
    return "unspecified"


def _chief_multi_source_context_workflow(
    text: str,
) -> tuple[list[ManualTargetAgent], bool]:
    """Compile explicitly named read sources for one Chief-owned synthesis.

    This compatibility fallback recognizes provider/context identities only.
    It does not infer a research owner from generic words such as ``review`` or
    ``recommend``. The live planner still owns semantic interpretation; this
    helper prevents clearly named context sources from being collapsed into an
    unrelated specialist workflow before the Chief can consult them as tools.
    """

    current_turn = _without_reported_quoted_text(str(text or ""))
    source_patterns: tuple[
        tuple[ManualTargetAgent, str | None, re.Pattern[str]], ...
    ] = (
        (
            "gmail_triage",
            "gmail",
            re.compile(r"\b(?:gmail|inbox|mailbox)\b", re.I),
        ),
        (
            "airtable_context_agent",
            "airtable",
            re.compile(r"\bairtable\b", re.I),
        ),
        (
            "google_workspace_context_agent",
            "google_workspace",
            re.compile(
                r"\b(?:google\s+workspace|google\s+drive|google\s+docs?|"
                r"google\s+sheets?|drive\s+folder)\b",
                re.I,
            ),
        ),
        (
            "zotero_context_agent",
            "zotero",
            re.compile(r"\bzotero\b", re.I),
        ),
        (
            "rss_context_agent",
            None,
            re.compile(r"\b(?:rss|feed\s+context|announcement\s+history)\b", re.I),
        ),
        (
            "preprints_context_agent",
            None,
            re.compile(r"\bpreprints?\b", re.I),
        ),
    )
    ordered: list[tuple[int, ManualTargetAgent]] = []
    for route, provider, pattern in source_patterns:
        if provider and _request_forbids_provider_access(text, provider=provider):
            continue
        match = pattern.search(current_turn)
        if match is not None:
            ordered.append((match.start(), route))
    work_items_requested = bool(
        re.search(r"\bwork\s*items?\b", current_turn, re.I)
    )
    if len(ordered) + int(work_items_requested) < 2:
        return [], False
    workflow = list(dict.fromkeys(route for _, route in sorted(ordered)))
    return workflow, work_items_requested


def _infer_goal_workflow(text: str) -> list[ManualTargetAgent]:
    """Infer an ordered multi-owner fallback from task shape, not agent names.

    The live manual planner is the primary interpretation layer. This bounded
    fallback only preserves an obvious sequence when one request contains two
    or more distinct deliverables.
    """

    cleaned = _without_negated_route_action_clauses(str(text or ""))
    cleaned = re.sub(
        r"\b(?:do\s+not|don't|dont|never|without|avoid|skip)\b"
        r"[^.;\n]*[.;]?",
        " ",
        cleaned,
        flags=re.I,
    )
    patterns: tuple[tuple[ManualTargetAgent, re.Pattern[str]], ...] = (
        (
            "gmail_triage",
            re.compile(
                r"\b(?:review|triage|summari[sz]e|extract|inspect)\b"
                r"[\s\S]{0,140}\b(?:gmail|email|inbox)\b"
                r"|\b(?:gmail|email|inbox)\b"
                r"[\s\S]{0,140}\b(?:review|triage|summari[sz]e|extract|inspect)\b",
                re.I,
            ),
        ),
        (
            "business_research_analyst",
            re.compile(
                r"\b(?:summari[sz]e|review|analy[sz]e|assess|explain|research)\b"
                r"[\s\S]{0,180}\b(?:architecture|trade-?offs?|evidence|facts?|"
                r"research|context|company|organization|topic|source|packet|"
                r"supported|established|known\s+and\s+unknown|unknowns?)\b"
                r"|\bwhat\b[\s\S]{0,80}\b(?:is|are)\b[\s\S]{0,80}"
                r"\b(?:supported|established|known|unknown)\b",
                re.I,
            ),
        ),
        (
            "opportunity_scout",
            re.compile(
                r"\b(?:identify|prioriti[sz]e|rank|recommend|select|choose|find)\b"
                r"[\s\S]{0,180}\b(?:validation\s+gaps?|gaps?|priorit(?:y|ies)|"
                r"opportunit(?:y|ies)|next\s+actions?|risks?|targets?|leads?)\b"
                r"|\b(?:assess|evaluate|decide|determine)\b"
                r"[\s\S]{0,90}\bopportunit(?:y|ies)\b"
                r"|\b(?:strongest|best|highest[- ]value|most\s+credible)\b"
                r"[\s\S]{0,120}\b(?:advisory|research|collaboration|partnership)\b"
                r"[\s\S]{0,50}\b(?:fit|opportunit(?:y|ies)|direction)\b",
                re.I,
            ),
        ),
        (
            "outreach_composer",
            re.compile(
                r"\b(?:draft|write|compose|prepare)\b[\s\S]{0,180}\b"
                r"(?:(?:internal\s+)?slack\s+(?:update|message|brief|note|recommendation)|"
                r"outreach|email|reply|response|follow-up|followup)\b"
                r"|\bpaste[- ]ready\s+(?:internal\s+)?slack\s+"
                r"(?:update|message|brief|note|recommendation)\b"
                r"|\b(?:short|concise)?\s*internal\s+slack\s+"
                r"(?:update|message|brief|note|recommendation)\b"
                r"[\s\S]{0,100}\b(?:copy|paste|review)\b",
                re.I,
            ),
        ),
    )
    ordered: list[tuple[int, ManualTargetAgent]] = []
    for route, pattern in patterns:
        if route == "gmail_triage" and _request_forbids_provider_access(
            text,
            provider="gmail",
        ):
            continue
        match = pattern.search(cleaned)
        if match is not None:
            ordered.append((match.start(), route))
    workflow = list(dict.fromkeys(route for _, route in sorted(ordered)))
    if is_single_owner_gmail_reply_request(text) and "gmail_triage" in workflow:
        # Gmail Triage owns both the bounded provider read and reply copy. A
        # Slack-thread-only draft is an output field, not a second Outreach
        # deliverable. Explicit research or another context source still keeps
        # the genuinely multi-owner workflow above.
        workflow = [route for route in workflow if route != "outreach_composer"]
    return workflow if len(workflow) > 1 else []


def _goal_workflow_primary_target(text: str) -> str:
    """Extract a concrete workflow subject without treating the whole ask as an entity."""

    cleaned = _strip_direct_agent_prefix(str(text or "")).strip()
    for pattern in (
        re.compile(
            r"\b(?:using|from|reviewing)\s+(?:the\s+)?"
            r"(?:approved|provided|supplied)\s+"
            r"(?P<name>[A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,4})\s+"
            r"(?:packet|brief|profile|context)\b"
        ),
        re.compile(
            r"^(?P<name>[A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,4})\s+"
            r"(?:has\s+been|is\s+an?|appears?\s+to\s+be)\b"
        ),
    ):
        match = pattern.search(cleaned)
        if match is not None:
            return match.group("name").strip(" .,:;-")[:120]
    return ""


def _requires_live_search_for_plan(text: str, *, target_agent: ManualTargetAgent) -> bool:
    if target_agent not in {"business_research_analyst", "opportunity_scout"}:
        return False
    if _looks_like_supplied_context_synthesis_request(text):
        return False
    return not request_forbids_live_research(text)


def request_forbids_live_research(text: str) -> bool:
    """Return whether the operator explicitly bounded work away from live research."""

    source = _without_reported_quoted_text(text)
    explicit_constraints = _explicit_negative_constraints(source)
    if any(_negative_constraint_forbids_live_search(item) for item in explicit_constraints):
        return True
    normalized = " ".join(source.lower().split())
    if re.search(
        r"\b(?:live\s+(?:web\s+)?search|web\s+search|external\s+(?:search|research)|"
        r"browser\s+automation)\b[^.;\n]{0,80}\b"
        r"(?:not\s+approved|not\s+allowed|not\s+permitted|disabled|off-limits)\b",
        normalized,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:use|using|based)\s+only(?:\s+on)?\b"
            r"[^.;\n]{0,160}\b"
            r"(?:context|note|packet|materials?|facts?|(?:selected\s+)?thread|email)\b",
            " ".join(source.split()),
            re.I,
        )
    )


def live_search_allowed_for_execution(
    enabled: bool,
    *,
    manual_plan: object | None,
    request_text: str,
) -> bool:
    """Resolve search intent once without letting prose override a canonical plan.

    The runtime flag controls whether live search is available. A canonical
    Orchestrator plan controls whether this task requests it. Raw wording is
    consulted only for planner-unavailable and compatibility execution.
    """

    if not enabled:
        return False
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical and authority.field_supplied("requires_live_search"):
        assert authority.plan is not None
        return authority.plan.requires_live_search
    if authority.invalid:
        return False
    return not request_forbids_live_research(request_text)


def _ask_shape_policy(text: str) -> AskShapePolicy:
    """Extract only explicit cross-cutting request constraints."""

    lower = " ".join(str(text or "").lower().split())
    positive_lower = " ".join(positive_capability_text(text).lower().split())
    output_constraints = _interpreted_output_constraints(text)
    local_attachment_context = has_explicit_local_attachment_context(text)
    exact = bool(
        re.search(
            r"\b(exact(?:ly)?|exact matches? only|no adjacent|no padding|do not broaden|"
            r"zero (?:results? )?if (?:there are )?none)\b",
            lower,
        )
    )
    selected_context = bool(
        re.search(
            r"\b(?:selected|supplied|provided|this) "
            r"(?:gmail )?(?:thread|source|context)\b"
            r"|\b(?:same|this|that) "
            r"(?:article|event|record|draft|document|doc|sheet|message|item|file)\b",
            lower,
        )
        or _has_supplied_context_boundary(lower)
        or _looks_like_inline_fact_packet_synthesis(text)
        or local_attachment_context
    )
    source_types = [
        label
        for phrase, label in (
            ("official sources", "official"),
            ("official source", "official"),
            ("primary sources", "primary"),
            ("primary source", "primary"),
            ("selected sources", "selected"),
            ("selected source", "selected"),
            ("provided sources", "provided"),
            ("provided source", "provided"),
            ("supplied sources", "provided"),
            ("supplied source", "provided"),
            ("local sources", "local"),
            ("local source", "local"),
        )
        if phrase in lower
    ]
    if local_attachment_context:
        source_types.append("local_attachment")
    source_types = list(dict.fromkeys(source_types))
    read_only = bool(
        re.search(
            r"\b(read[- ]only|(?:do\s+not|don't|dont|never) "
            r"(?:add|insert|send|post|create|remove|modify|change|write|publish|"
            r"draft|label|archive)|"
            r"without (?:adding|inserting|sending|posting|creating|removing|modifying|"
            r"changing|writing|publishing|drafting|labeling|archiving))\b",
            lower,
        )
        or re.search(
            r"\b(?:do\s+not|don't|dont|never) [^.]{0,80}\b"
            r"(?:add|insert|send|post|create|remove|modify|change|write|publish|"
            r"draft|label|archive)\b",
            lower,
        )
        or bool(re.search(r"\b(?:make|perform) no changes?\b|\bno changes?\b", lower))
    )
    draft_requested = bool(
        re.search(r"\b(draft|prepare (?:a )?reply|reply copy)\b", positive_lower)
    )
    approval_required = bool(
        re.search(r"\b(?:after|before|pending|requires?) (?:human )?approval\b", lower)
        or "only after approval" in lower
    )
    stop_condition = ""
    if exact and re.search(r"\b(zero .* if .*none|do not broaden|no adjacent|no padding)\b", lower):
        stop_condition = "return_zero_without_broadening_if_no_exact_match"
    elif approval_required:
        stop_condition = "stop_before_external_action_until_approval"
    elif output_constraints.word_count is not None:
        stop_condition = f"stop_after_{output_constraints.word_count}_word_summary"
    elif output_constraints.sentence_count is not None:
        stop_condition = "stop_after_exact_requested_sentence_count"
    elif "stop after" in lower:
        stop_condition = "honor_explicit_stop_after_boundary"

    return AskShapePolicy(
        ask_breadth=(
            "narrow"
            if selected_context or "only" in lower
            else "bounded"
            if exact or re.search(r"\b(?:find|return|show) [1-9]\d?\b", lower)
            else "broad"
            if re.search(r"\b(broad|comprehensive|all relevant)\b", lower)
            else "unspecified"
        ),
        evidence_depth=(
            "quick"
            if re.search(r"\b(quick|brief|concise|do not deepen)\b", lower)
            else "deep"
            if re.search(r"\b(deep|comprehensive|thorough|detailed)\b", lower)
            else "unspecified"
        ),
        source_type_preference=source_types,
        strict_filter_mode=(
            "exact"
            if exact
            else "strict"
            if re.search(r"\b(strict|hard filters?)\b", lower)
            else "flexible"
            if re.search(r"\b(adjacent matches? (?:are )?(?:ok|acceptable)|broaden)\b", lower)
            else "unspecified"
        ),
        output_form=(
            "brief"
            if output_constraints.sentence_count is not None
            else "table"
            if re.search(r"\btable\b", lower)
            else "bullets"
            if re.search(r"\b(?:bullets?|bulleted|talking\s+points?)\b", lower)
            else "plan"
            if re.search(r"\b(?:plan|next steps)\b", lower)
            else "draft"
            if draft_requested
            else "brief"
            if re.search(r"\b(?:brief|summary|summarize|summarise|concise)\b", lower)
            else "unspecified"
        ),
        prior_context_dependency=(
            "none"
            if re.search(r"\b(?:ignore|do not use) (?:the )?(?:prior|previous) context\b", lower)
            else "selected_context"
            if selected_context
            else "required"
            if re.search(
                r"\b(?:prior|previous|earlier|above) (?:context|thread|result|work)\b", lower
            )
            else "unspecified"
        ),
        permission_state=(
            "approval_required"
            if approval_required
            else "draft_only"
            if draft_requested and (read_only or "do not send" in lower)
            else "read_only"
            if read_only
            else "unspecified"
        ),
        audience_scope=_explicit_audience_scope(lower),
        cost_mode=(
            "minimize"
            if re.search(r"\b(low[- ]cost|minimi[sz]e cost|quick|cheap)\b", lower)
            else "quality"
            if re.search(r"\b(best quality|deep|thorough)\b", lower)
            else "unspecified"
        ),
        stop_condition=stop_condition,
        output_constraints=output_constraints,
    )


def _interpreted_output_constraints(text: str) -> InterpretedOutputConstraints:
    """Provide an offline fallback for explicit measurable response constraints.

    Live natural-language entrypoints let the LLM planner own this interpretation.
    This parser keeps dry-run and provider-failure behavior safe and testable.
    """

    normalized = " ".join(str(text or "").split())
    lower = normalized.lower()
    word_match = re.search(
        r"\b(?P<mode>exactly|in|no\s+more\s+than|at\s+most|under|within|"
        r"max(?:imum)?(?:\s+of)?|minimum(?:\s+of)?|at\s+least)\s+"
        r"(?P<count>[1-9]\d{0,3})\s+words?\b",
        lower,
    )
    sentence_mode_matches = list(
        re.finditer(
            r"\b(?P<mode>exactly|no\s+more\s+than|at\s+most|under|within|"
            r"max(?:imum)?(?:\s+of)?|minimum(?:\s+of)?|at\s+least)\s+"
            r"(?P<count>[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?:concise\s+|short\s+|brief\s+){0,2}sentences?\b",
            lower,
        )
    )
    sentence_exact_matches = list(
        re.finditer(
            r"\b(?P<count>[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?:concise\s+|short\s+|brief\s+|internal\s+|slack\s+|team\s+){0,4}"
            r"sentences?\b",
            lower,
        )
    )

    def count_match_has_response_shape_authority(match: re.Match[str] | None) -> bool:
        """Require a grammatical link between a count and the requested response.

        Counts reported inside source material, policies, titles, or examples are
        task content. They must not become a validator for the agent's answer.
        """

        if match is None:
            return False
        prefix = lower[max(0, match.start() - 100) : match.start()]
        suffix = lower[match.end() : match.end() + 100]
        if re.match(
            r"\s+(?:above\b|into\b|to\s+(?:become|write|create|make|produce|"
            r"form|draft|summari[sz]e)\b)",
            suffix,
        ):
            return False
        if re.match(
            r"\s+(?:are|is|was|were|may|must|can|could|should|would)\b",
            suffix,
        ):
            return False
        request_verb = (
            r"(?:answer|respond|reply|return|give(?:\s+me)?|provide|write|"
            r"summari[sz]e|explain|describe|keep|limit|shorten|condense|"
            r"turn|convert|reformat|combine)"
        )
        prefix_clause = re.split(r"[.;:!?\n]", prefix)[-1]
        if re.search(rf"\b{request_verb}\b[^.;:!?\n]{{0,45}}$", prefix_clause):
            return True
        if re.match(r"\s+only\b", suffix):
            return True
        return bool(
            re.match(
                rf"\s*[,;:\-]?\s*{request_verb}\b",
                suffix,
            )
        )

    word_match = word_match if count_match_has_response_shape_authority(word_match) else None
    grounded_sentence_mode_matches = [
        match for match in sentence_mode_matches if count_match_has_response_shape_authority(match)
    ]
    grounded_sentence_exact_matches = [
        match for match in sentence_exact_matches if count_match_has_response_shape_authority(match)
    ]

    def parsed_count(match: re.Match[str]) -> int | None:
        raw = match.group("count").lower()
        return int(raw) if raw.isdigit() else _COUNT_WORDS.get(raw)

    grounded_sentence_counts = {
        count
        for count in (
            parsed_count(match)
            for match in [
                *grounded_sentence_mode_matches,
                *grounded_sentence_exact_matches,
            ]
        )
        if count is not None
    }
    # Two different current-turn sentence counts are ambiguous. Keep them as
    # planner-visible prose, but do not turn either into a blocker-grade rule.
    sentence_mode_match = (
        grounded_sentence_mode_matches[0]
        if len(grounded_sentence_counts) == 1 and grounded_sentence_mode_matches
        else None
    )
    sentence_exact_match = (
        grounded_sentence_exact_matches[0]
        if len(grounded_sentence_counts) == 1 and grounded_sentence_exact_matches
        else None
    )
    sentence_match = sentence_mode_match or sentence_exact_match

    item_range = re.search(
        r"\b(?P<minimum>[1-9]\d?)\s*[-\u2013]\s*(?P<maximum>[1-9]\d?)\s+"
        r"(?:bullets?|items?|results?|options?|recommendations?)\b",
        lower,
    )
    item_range = item_range if count_match_has_response_shape_authority(item_range) else None
    item_exact_matches = list(
        re.finditer(
            r"\b(?:exactly\s+)?"
            r"(?P<count>[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?:[a-z][\w-]*\s+){0,2}"
            r"(?P<item_kind>bullets?|items?|results?|options?|recommendations?|"
            r"points?|talking\s+points?)\b",
            lower,
        )
    )

    def item_match_has_response_shape_authority(match: re.Match[str]) -> bool:
        """Reject task-local singular choices as whole-response cardinality.

        A counted phrase is authoritative only when its grammar describes the
        response. References such as "turn those two bullets into one sentence"
        describe source material and must not become a two-item validator.
        """

        suffix = lower[match.end() : match.end() + 80]
        if re.match(r"\s+(?:above\b|into\b|to\s+become\b)", suffix):
            return False
        matched_text = match.group(0)
        prefix = lower[max(0, match.start() - 120) : match.start()]
        source_reference = re.search(
            r"\b(?:these|those|existing|previous|prior)\s*$",
            prefix,
        )
        if source_reference and not re.search(
            r"\b(?:return|keep|preserve|repeat)\b[^.;:!?\n]{0,45}$",
            prefix,
        ):
            return False
        if matched_text.startswith("exactly ") and not source_reference:
            return True
        response_shape_verb = re.search(
            r"\b(?:return|give(?:\s+me)?|list|provide|include|show|write|"
            r"respond\s+with|reply\s+with|keep|preserve|repeat|"
            r"format(?:\s+\w+){0,3}\s+as|turn(?:\s+\w+){0,5}\s+into)\s+"
            r"(?:only\s+|exact\s+|exactly\s+|the\s+|same\s+){0,3}$",
            prefix,
        )
        response_shape_preposition = re.search(
            r"\b(?:in|as|into)\s+(?:only\s+|exactly\s+){0,2}$",
            prefix,
        )
        trailing_only = re.match(r"\s+only\b", lower[match.end() :])
        return bool(response_shape_verb or response_shape_preposition or trailing_only)

    grounded_item_exact_matches = [
        match for match in item_exact_matches if item_match_has_response_shape_authority(match)
    ]
    grounded_item_counts = {
        count
        for count in (parsed_count(match) for match in grounded_item_exact_matches)
        if count is not None
    }
    authoritative_item_exact = (
        grounded_item_exact_matches[0]
        if len(grounded_item_counts) == 1 and grounded_item_exact_matches
        else None
    )

    def count_mode(label: str) -> str:
        if label in {"exactly", "in"}:
            return "exact"
        if label == "under":
            return "under"
        if label in {"minimum of", "at least"}:
            return "minimum"
        return "maximum"

    required_sections: list[str] = []
    section_match = re.search(r"\breturn\s+exactly\s*:\s*(?P<sections>[^.\n]+)", normalized, re.I)
    if section_match:
        required_sections = [
            item.strip(" -*`\t")
            for item in re.split(r",|\band\b", section_match.group("sections"), flags=re.I)
            if item.strip(" -*`\t")
        ][:10]
    forbidden_phrases = list(
        dict.fromkeys(
            match.group("phrase").strip()
            for match in re.finditer(
                r"\b(?:do\s+not|don't|never|avoid)\s+"
                r"(?:say|use|include|mention|write)\s+"
                r"(?:(?:the\s+)?(?:word|phrase|term)\s+)?"
                r"[\"“'](?P<phrase>[^\"”']{1,80})[\"”']",
                normalized,
                re.I,
            )
            if match.group("phrase").strip()
        )
    )

    word_count = int(word_match.group("count")) if word_match else None
    sentence_count = None
    if sentence_match:
        raw_sentence_count = sentence_match.group("count").lower()
        sentence_count = (
            int(raw_sentence_count)
            if raw_sentence_count.isdigit()
            else _COUNT_WORDS.get(raw_sentence_count)
        )
    minimum_items = int(item_range.group("minimum")) if item_range else None
    maximum_items = int(item_range.group("maximum")) if item_range else None
    if authoritative_item_exact and not item_range:
        raw_item_count = authoritative_item_exact.group("count").lower()
        parsed_item_count = (
            int(raw_item_count) if raw_item_count.isdigit() else _COUNT_WORDS.get(raw_item_count)
        )
        singular_word_recommendation = bool(
            raw_item_count == "one"
            and authoritative_item_exact.group("item_kind").lower() == "recommendation"
        )
        if parsed_item_count is not None and not singular_word_recommendation:
            minimum_items = maximum_items = parsed_item_count

    explicit = bool(
        word_match
        or sentence_match
        or item_range
        or authoritative_item_exact
        or required_sections
        or forbidden_phrases
        or re.search(r"\b(?:no|do not use|avoid)\s+em\s+dashes?\b", lower)
        or re.search(r"\b(?:cite|include|show|provide)\b[^.]{0,60}\b(?:urls?|links?)\b", lower)
    )
    scope = (
        "answer"
        if word_match or sentence_match
        else "entire_response"
        if explicit
        else "unspecified"
    )
    interpretation_parts: list[str] = []
    if word_match:
        interpretation_parts.append(
            f"{count_mode(word_match.group('mode'))} {word_count}-word answer"
        )
    if sentence_match:
        interpretation_parts.append(
            f"{count_mode(sentence_mode_match.group('mode')) if sentence_mode_match else 'exact'} "
            f"{sentence_count}-sentence answer"
        )
    if minimum_items is not None or maximum_items is not None:
        interpretation_parts.append(
            f"item count {minimum_items if minimum_items is not None else 0}"
            f"-{maximum_items if maximum_items is not None else 'unbounded'}"
        )
    if required_sections:
        interpretation_parts.append("required sections: " + ", ".join(required_sections))
    return InterpretedOutputConstraints(
        interpretation="; ".join(interpretation_parts),
        scope=scope,
        word_count_mode=count_mode(word_match.group("mode")) if word_match else "unspecified",
        word_count=word_count,
        sentence_count_mode=(
            count_mode(sentence_mode_match.group("mode"))
            if sentence_mode_match
            else "exact"
            if sentence_exact_match
            else "unspecified"
        ),
        sentence_count=sentence_count,
        item_count_mode=(
            "exact"
            if minimum_items is not None and minimum_items == maximum_items
            else "maximum"
            if maximum_items is not None and minimum_items is None
            else "minimum"
            if minimum_items is not None and maximum_items is None
            else "unspecified"
        ),
        minimum_items=minimum_items,
        maximum_items=maximum_items,
        required_sections=required_sections,
        require_section_headings=bool(required_sections),
        forbidden_phrases=forbidden_phrases,
        forbid_em_dash=bool(re.search(r"\b(?:no|do not use|avoid)\s+em\s+dashes?\b", lower)),
        include_source_urls=bool(
            re.search(r"\b(?:cite|include|show|provide)\b[^.]{0,60}\b(?:urls?|links?)\b", lower)
        ),
    )


def _reconcile_current_turn_ask_shape(
    base: AskShapePolicy,
    candidate: AskShapePolicy,
    *,
    prefer_candidate_output_constraints: bool = False,
    request_text: str = "",
) -> AskShapePolicy:
    """Build one canonical output contract from the authoritative current turn.

    Semantic planning may add interpretation and style. Only hard requirements
    grounded by the current-turn parser can reach deterministic validators; prior
    thread shapes and planner-only counts remain advisory.
    """

    resolved_candidate = AskShapePolicy.model_validate(candidate)
    values = resolved_candidate.model_dump(mode="json")
    preserved_fields = (
        (
            "strict_filter_mode",
            "output_form",
        )
        if prefer_candidate_output_constraints
        else (
            "ask_breadth",
            "evidence_depth",
            "strict_filter_mode",
            "output_form",
            "prior_context_dependency",
            "permission_state",
            "audience_scope",
            "cost_mode",
        )
    )
    for field_name in preserved_fields:
        base_value = getattr(base, field_name)
        if base_value != "unspecified":
            values[field_name] = base_value
    # A plainly stated internal/external audience belongs to the authoritative
    # current turn even when the LLM owns the rest of semantic interpretation.
    # This field may relax an external-drafting prerequisite, but it cannot
    # grant a provider action.
    if base.audience_scope != "unspecified":
        values["audience_scope"] = base.audience_scope
    if prefer_candidate_output_constraints and base.permission_state == "approval_required":
        values["permission_state"] = "approval_required"
    values["source_type_preference"] = list(
        dict.fromkeys([*base.source_type_preference, *resolved_candidate.source_type_preference])
    )
    if base.stop_condition:
        values["stop_condition"] = base.stop_condition
    base_constraints = base.output_constraints
    candidate_constraints = resolved_candidate.output_constraints
    if prefer_candidate_output_constraints and candidate_constraints.is_explicit():
        candidate_values = candidate_constraints.model_dump(mode="json")
        # The planner may explain or refine an explicit response rule, but it
        # cannot invent hard validator authority. Only the high-precision local
        # parser can promote operator wording into counts, exact forbidden
        # phrases, punctuation, or source-URL requirements. Planner-supplied
        # section labels are retained only when every label is grounded in the
        # raw request; this lets semantic planning preserve distinct named
        # deliverables without turning arbitrary planner prose into a blocker.
        # This keeps provider-action constraints such as "do not create a Gmail
        # draft" from becoming bans on the visible word "draft".
        for mode_field, count_field in (
            ("word_count_mode", "word_count"),
            ("sentence_count_mode", "sentence_count"),
        ):
            base_count = getattr(base_constraints, count_field)
            if base_count is None:
                candidate_values[mode_field] = "unspecified"
                candidate_values[count_field] = None
            else:
                candidate_values[count_field] = base_count
                if candidate_values.get(mode_field) == "unspecified":
                    candidate_values[mode_field] = getattr(base_constraints, mode_field)
        grounded_required_sections = _grounded_planner_required_sections(
            request_text,
            candidate_constraints.required_sections,
        )
        retained_required_sections = list(
            dict.fromkeys(
                [
                    *base_constraints.required_sections,
                    *grounded_required_sections,
                ]
            )
        )
        candidate_values.update(
            {
                "item_count_mode": base_constraints.item_count_mode,
                "minimum_items": base_constraints.minimum_items,
                "maximum_items": base_constraints.maximum_items,
                "required_sections": retained_required_sections,
                "require_section_headings": bool(
                    retained_required_sections
                    and (
                        base_constraints.require_section_headings
                        or (
                            candidate_constraints.require_section_headings
                            and grounded_required_sections
                        )
                    )
                ),
                "forbidden_phrases": list(base_constraints.forbidden_phrases),
                "forbid_em_dash": base_constraints.forbid_em_dash,
                "include_source_urls": base_constraints.include_source_urls,
            }
        )
        if base_constraints.has_deterministic_requirements():
            candidate_values["scope"] = base_constraints.scope
        values["output_constraints"] = candidate_values
    elif base_constraints.is_explicit():
        values["output_constraints"] = base_constraints.model_dump(mode="json")
    return AskShapePolicy.model_validate(values)


def _planner_hard_output_contract_differs(
    grounded: InterpretedOutputConstraints,
    candidate: InterpretedOutputConstraints,
) -> bool:
    """Report planner hard fields that cannot become current-turn authority."""

    count_fields = (
        "word_count",
        "sentence_count",
        "minimum_items",
        "maximum_items",
    )
    if any(
        getattr(candidate, field_name) is not None
        and getattr(candidate, field_name) != getattr(grounded, field_name)
        for field_name in count_fields
    ):
        return True
    return bool(
        set(candidate.forbidden_phrases) - set(grounded.forbidden_phrases)
        or (candidate.forbid_em_dash and not grounded.forbid_em_dash)
        or (candidate.include_source_urls and not grounded.include_source_urls)
    )


_SECTION_GROUNDING_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "answer",
        "concise",
        "for",
        "of",
        "part",
        "response",
        "section",
        "short",
        "the",
        "to",
    }
)


def _grounded_planner_required_sections(
    request_text: str,
    sections: list[str],
) -> list[str]:
    """Accept semantic section labels only when the operator named their substance."""

    cleaned_sections = list(
        dict.fromkeys(
            str(section or "").strip() for section in sections if str(section or "").strip()
        )
    )[:10]
    if len(cleaned_sections) < 2:
        return []
    request_tokens = set(re.findall(r"[a-z0-9]+", str(request_text or "").lower()))
    grounded: list[str] = []
    for section in cleaned_sections:
        section_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", section.lower())
            if len(token) > 1 and token not in _SECTION_GROUNDING_STOPWORDS
        ]
        if not section_tokens:
            return []
        overlap = sum(token in request_tokens for token in section_tokens)
        if overlap < max(1, (len(section_tokens) + 1) // 2):
            return []
        grounded.append(section)
    return grounded


def merge_manual_request_plan(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan | dict[str, Any] | None,
    *,
    allow_contextual_delegation: bool = False,
) -> ManualRequestPlan:
    """Merge semantic interpretation over a planner-unavailable fallback.

    The LLM plan owns meaning: route, workflow, provider, operations, and
    durability. The local plan may preserve explicit measurable constraints
    and safety boundaries, but it must not veto a valid LLM interpretation
    because the request used wording that a heuristic did not recognize.
    """

    if candidate is None:
        return base
    plan = (
        candidate
        if isinstance(candidate, ManualRequestPlan)
        else ManualRequestPlan.model_validate(candidate)
    )
    llm_interpretation = plan.source == "llm"
    if not llm_interpretation:
        preserved_intents = {"opportunity_to_outreach_loop", "browser_diagnostics"}
        if base.intent in preserved_intents and (
            plan.intent != base.intent or plan.target_agent != base.target_agent
        ):
            warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
            warnings.append(
                "Ignored non-LLM override that converted a protected manual request "
                "into a different route."
            )
            return base.model_copy(
                update={
                    "source": plan.source or base.source,
                    "planner_warnings": warnings,
                }
            )
        if _candidate_uses_explicitly_forbidden_capability(base, plan):
            warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
            warnings.append(
                "Ignored non-LLM override that treated an explicitly forbidden "
                "capability as positive routing evidence."
            )
            return base.model_copy(
                update={
                    "source": plan.source or base.source,
                    "planner_warnings": warnings,
                }
            )
        if _candidate_turns_constraint_into_prerequisite_or_blocker(base, plan):
            warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
            warnings.append(
                "Ignored non-LLM override that turned a negative execution constraint "
                "into a prerequisite, clarification, or workflow blocker."
            )
            return base.model_copy(
                update={
                    "source": plan.source or base.source,
                    "planner_warnings": warnings,
                }
            )
        explicit_agent = base.requested_agent not in {None, "", "orchestrator"}
        contextual_delegation = bool(
            allow_contextual_delegation
            and base.requested_agent == "chief_of_staff"
            and plan.target_agent in _CONTEXT_AGENT_TARGETS
        )
        if explicit_agent and plan.target_agent != base.target_agent and not contextual_delegation:
            warnings = list(dict.fromkeys([*base.planner_warnings, *plan.planner_warnings]))
            warnings.append(
                "Ignored non-LLM override that rerouted an explicit named-agent request."
            )
            return base.model_copy(
                update={
                    "source": plan.source or base.source,
                    "planner_warnings": warnings,
                }
            )
    else:
        plan = _normalize_llm_plan_contract(plan)
        plan = _repair_structurally_invalid_llm_plan(base, plan)
        plan = _prune_forbidden_llm_capabilities(base, plan)
        # Normalization can expose a second structural contradiction (for
        # example, pruning a forbidden workflow can make the remaining owner
        # incompatible with the intent). Run the bounded repair pipeline to a
        # second fixed-point pass; this is schema repair, not phrase routing.
        plan = _normalize_llm_plan_contract(plan)
        plan = _repair_structurally_invalid_llm_plan(base, plan)
        plan = _prune_forbidden_llm_capabilities(base, plan)
    candidate_values = plan.model_dump(mode="json")
    if base.requested_agent not in {None, ""}:
        # ``requested_agent`` records the human entry surface. The semantic
        # planner may select a better execution owner in ``target_agent``, but
        # it must not rewrite which named agent the operator addressed. Keeping
        # these fields distinct also lets later review explain a delegation
        # without treating it as a new user instruction.
        candidate_values["requested_agent"] = base.requested_agent
    if (
        not llm_interpretation
        and candidate_values.get("provider_system") == "unspecified"
        and base.provider_system != "unspecified"
    ):
        candidate_values["provider_system"] = base.provider_system
    merged = base.model_copy(update=candidate_values)
    merged.provider_action_steps = [
        (
            step
            if isinstance(step, ManualProviderActionStep)
            else ManualProviderActionStep.model_validate(step)
        )
        for step in merged.provider_action_steps
    ]
    if (
        llm_interpretation
        and base.provider_system == "zotero"
        and merged.provider_system == "zotero"
    ):
        if base.provider_selection_order != "unspecified":
            merged.provider_selection_order = base.provider_selection_order
        if base.provider_selection_rank is not None:
            merged.provider_selection_rank = base.provider_selection_rank
        if base.zotero_requested_fields:
            if base.ask_shape.strict_filter_mode in {"exact", "strict"}:
                merged.zotero_requested_fields = list(base.zotero_requested_fields)
            else:
                merged.zotero_requested_fields = list(
                    dict.fromkeys(
                        [
                            *merged.zotero_requested_fields,
                            *base.zotero_requested_fields,
                        ]
                    )
                )
    planner_contract_reconciled = bool(
        llm_interpretation
        and _planner_hard_output_contract_differs(
            base.ask_shape.output_constraints,
            plan.ask_shape.output_constraints,
        )
    )
    merged.ask_shape = _reconcile_current_turn_ask_shape(
        base.ask_shape,
        plan.ask_shape,
        prefer_candidate_output_constraints=plan.source == "llm",
        request_text=base.objective,
    )
    bounded_local_attachment_response = bool(
        has_explicit_local_attachment_context(base.objective)
        and _looks_like_supplied_context_synthesis_request(base.objective)
        and not looks_like_stateful_work_request(base.objective)
        and not provider_tool_action_bound(base.objective)
        and merged.target_agent in _DIRECT_RESPONSE_TARGETS
        and not merged.workflow
    )
    if bounded_local_attachment_response and (
        merged.provider_system != "unspecified" or merged.provider_operations
    ):
        merged.provider_system = "unspecified"
        merged.provider_operations = []
        merged.provider_action_steps = []
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Ignored an unrequested provider read because the selected local "
                    "attachment already supplies the bounded evidence.",
                ]
            )
        )
    if planner_contract_reconciled:
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Reconciled planner hard-output fields to the constraints grounded "
                    "in the authoritative current turn; ungrounded counts remain advisory.",
                ]
            )
        )
    if not merged.requested_agent:
        merged.requested_agent = base.requested_agent
    if not merged.primary_target:
        merged.primary_target = base.primary_target
    if not merged.objective:
        merged.objective = base.objective
    if llm_interpretation:
        explicit_safety_constraints = _explicit_negative_constraints(base.objective)
        merged.constraints = list(
            dict.fromkeys([*merged.constraints, *explicit_safety_constraints])
        )
    else:
        merged.constraints = list(dict.fromkeys([*base.constraints, *merged.constraints]))
        merged.required_entities = list(
            dict.fromkeys([*base.required_entities, *merged.required_entities])
        )
        merged.required_terms = list(dict.fromkeys([*base.required_terms, *merged.required_terms]))
    # A live LLM plan normally owns mailbox meaning and scope. The fallback
    # parser may populate these fields only when no LLM interpretation exists.
    #
    # Known-relationship contact recovery is a narrower contract: once both
    # layers agree that the current request is a Gmail contact lookup, the
    # distinctive organization/account term and any explicit date boundary
    # can be extracted exactly from the authoritative current turn. Preserve
    # those bounded fields so an LLM cannot accidentally turn ``"Acme
    # Compute"`` into Gmail's cumulative ``Acme Compute startup account
    # setup`` query or invent an unrequested mailbox direction/date filter.
    # This reconciles provider query shape; it does not choose the semantic
    # route when the planner selected a different task.
    bounded_known_contact_lookup = bool(
        llm_interpretation
        and base.task_objective == "contact_discovery"
        and merged.task_objective == "contact_discovery"
        and base.provider_system == "gmail"
        and merged.provider_system == "gmail"
        and base.gmail_query
    )
    if not llm_interpretation:
        if base.gmail_query:
            merged.gmail_query = base.gmail_query
        if base.lookback_days is not None:
            merged.lookback_days = base.lookback_days
        if base.draft_policy:
            merged.draft_policy = base.draft_policy
    elif bounded_known_contact_lookup:
        merged.primary_target = base.primary_target
        merged.provider_read_scope = base.provider_read_scope
        merged.provider_result_mode = base.provider_result_mode
        merged.gmail_mailbox_direction = base.gmail_mailbox_direction
        merged.gmail_date_scope = base.gmail_date_scope
        merged.gmail_requested_fields = list(base.gmail_requested_fields)
        merged.gmail_query = base.gmail_query
        merged.lookback_days = base.lookback_days
        if base.draft_policy == "no_drafts_requested":
            merged.draft_policy = "no_drafts_requested"
            if merged.ask_shape.output_form == "draft":
                merged.ask_shape = merged.ask_shape.model_copy(
                    update={"output_form": base.ask_shape.output_form}
                )
            merged.provider_operations = [
                operation
                for operation in merged.provider_operations
                if operation not in {"create", "update"}
            ]
            merged.provider_action_steps = [
                step
                for step in merged.provider_action_steps
                if step.operation not in {"create", "update"}
            ]
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Reconciled the known-contact Gmail query to the distinctive "
                    "organization/account term and explicit time scope in the "
                    "authoritative current turn.",
                ]
            )
        )
    elif base.draft_policy == "no_drafts_requested" and (
        merged.provider_system == "gmail"
        or merged.target_agent == "gmail_triage"
        or merged.intent == "gmail_triage"
    ):
        merged.draft_policy = "no_drafts_requested"
        if merged.ask_shape.output_form == "draft":
            merged.ask_shape = merged.ask_shape.model_copy(
                update={"output_form": base.ask_shape.output_form}
            )
        merged.provider_operations = [
            operation
            for operation in merged.provider_operations
            if operation not in {"create", "update"}
        ]
        merged.provider_action_steps = [
            step
            for step in merged.provider_action_steps
            if step.operation not in {"create", "update"}
        ]
    if (
        llm_interpretation
        and base.gmail_date_scope != "unspecified"
        and (
            merged.provider_system == "gmail"
            or merged.target_agent == "gmail_triage"
            or "gmail_triage" in merged.workflow
        )
    ):
        merged.gmail_date_scope = base.gmail_date_scope
    if base.gmail_exclude_threads_with_operator_reply and (
        merged.provider_system == "gmail"
        or merged.target_agent == "gmail_triage"
        or "gmail_triage" in merged.workflow
    ):
        merged.gmail_exclude_threads_with_operator_reply = True
    if not merged.recipient:
        merged.recipient = base.recipient
    if not merged.outreach_channel:
        merged.outreach_channel = base.outreach_channel
    if not merged.tone:
        merged.tone = base.tone
    base_workflow = list(base.workflow)
    candidate_workflow = list(merged.workflow)
    bounded_chief_multi_source_context = (
        _bounded_chief_multi_source_context_obligation(base)
    )
    bounded_provider_to_internal_draft = (
        _bounded_provider_to_internal_draft_obligation(base)
    )
    supplied_context_chief_resolution = bool(
        not llm_interpretation and _candidate_resolves_supplied_context_with_chief(base, plan)
    )
    if (
        supplied_context_chief_resolution
        and not base.requires_approved_context
        and plan.requires_approved_context
    ):
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Ignored planner override that turned a negative execution constraint "
                    "into a prerequisite, clarification, or workflow blocker.",
                ]
            )
        )
    if bounded_chief_multi_source_context:
        # Explicitly named read sources and open WorkItem state are measurable
        # current-turn deliverables. The LLM still interprets and synthesizes
        # evidence within each admitted stage, but a single-provider plan
        # cannot erase the other sources or replace the Chief as manager.
        merged.target_agent = base.target_agent
        merged.workflow = base_workflow
        merged.intent = base.intent
        merged.primary_target = base.primary_target
        merged.target_type = base.target_type
        merged.provider_system = base.provider_system
        merged.provider_operations = list(base.provider_operations)
        merged.provider_action_steps = list(base.provider_action_steps)
        merged.provider_read_scope = base.provider_read_scope
        merged.provider_result_mode = base.provider_result_mode
        merged.gmail_mailbox_direction = base.gmail_mailbox_direction
        merged.gmail_date_scope = base.gmail_date_scope
        merged.gmail_requested_fields = list(base.gmail_requested_fields)
        merged.task_objective = base.task_objective
        merged.expected_artifact_type = base.expected_artifact_type
        merged.requires_durable_state = True
        merged.requires_approved_context = base.requires_approved_context
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Preserved the current turn's Chief-owned multi-source read "
                    "contract after the planner collapsed it to one provider.",
                ]
            )
        )
    elif bounded_provider_to_internal_draft:
        # The current turn explicitly asks for both a bounded provider read and
        # an internal draft artifact. The LLM still owns semantic selection
        # within those stages, but it cannot silently erase either deliverable
        # and strand execution at a direct-draft context gate.
        merged.target_agent = base.target_agent
        merged.workflow = base_workflow
        merged.intent = base.intent
        merged.task_objective = base.task_objective
        merged.expected_artifact_type = base.expected_artifact_type
        merged.provider_system = base.provider_system
        merged.provider_operations = list(base.provider_operations)
        merged.provider_read_scope = base.provider_read_scope
        merged.provider_result_mode = base.provider_result_mode
        merged.gmail_mailbox_direction = base.gmail_mailbox_direction
        merged.gmail_date_scope = base.gmail_date_scope
        merged.outreach_channel = base.outreach_channel
        merged.recipient = base.recipient
        merged.requires_approved_context = base.requires_approved_context
        merged.missing_required_information = list(
            base.missing_required_information
        )
        merged.planner_warnings = list(
            dict.fromkeys(
                [
                    *merged.planner_warnings,
                    "Preserved the current turn's bounded provider-read and internal-draft "
                    "deliverables after the planner omitted one execution stage.",
                ]
            )
        )
    elif not llm_interpretation and base_workflow and not supplied_context_chief_resolution:
        # The local workflow inference is a bounded completeness check over
        # explicit deliverables in the raw request. The live planner enriches
        # the plan, but it must not silently drop a requested downstream owner
        # or turn Chief of Staff into a specialist execution step.
        merged.workflow = base_workflow
    else:
        merged.workflow = candidate_workflow if len(candidate_workflow) > 1 else []
    if len(merged.workflow) > 1:
        merged.requires_durable_state = True
    if base.desired_count_explicit:
        merged.desired_count = base.desired_count
        merged.desired_count_explicit = True
    merged.desired_count = max(1, min(10, merged.desired_count or base.desired_count))
    merged.side_effect_policy = (
        "internal_write_approval_required"
        if merged.intent == "business_system_write"
        else "draft_or_read_only"
    )
    if is_internal_slack_composition_plan(merged):
        # Internal copy is a draft artifact returned to the operator, not an
        # external outreach action. The selected facts are already the bounded
        # context, so external-claim approval must not become an execution
        # prerequisite.
        merged.requires_approved_context = False
        if merged.provider_system == "slack" and not merged.provider_operations:
            # A planner may describe the intended response surface as Slack.
            # Without a typed Slack operation, that is an audience description,
            # not provider authority.
            merged.provider_system = "unspecified"
    elif merged.target_agent == "outreach_composer" or (
        not llm_interpretation and base.target_agent == "outreach_composer"
    ):
        merged.requires_approved_context = True
    elif supplied_context_chief_resolution:
        merged.requires_approved_context = False
    return _apply_typed_provider_permission_ceiling(merged)


def _bounded_chief_multi_source_context_obligation(
    plan: ManualRequestPlan,
) -> bool:
    """Recognize a typed Chief synthesis that must retain every read source."""

    read_operations = set(plan.provider_operations)
    return bool(
        plan.requested_agent == "chief_of_staff"
        and plan.target_agent == "chief_of_staff"
        and plan.intent == "context_lookup"
        and plan.task_objective == "context_lookup"
        and plan.expected_artifact_type == "context_summary"
        and plan.target_type == "business_system_context"
        and plan.ask_shape.permission_state == "read_only"
        and read_operations
        and read_operations <= {"read", "search", "verify"}
        and (
            len(plan.workflow) >= 2
            or (len(plan.workflow) == 1 and plan.requires_durable_state)
        )
    )


def _bounded_provider_to_internal_draft_obligation(
    plan: ManualRequestPlan,
) -> bool:
    """Recognize a complete read-then-draft contract from typed current-turn fields."""

    operations = set(plan.provider_operations)
    return bool(
        len(plan.workflow) > 1
        and plan.requires_durable_state
        and plan.provider_system != "unspecified"
        and operations
        and operations <= {"read", "search", "verify"}
        and bool(operations & {"read", "search"})
        and plan.provider_read_scope == "bounded_collection"
        and plan.provider_result_mode == "items"
        and plan.expected_artifact_type == "outreach_draft"
        and plan.task_objective == "outreach_draft"
        and plan.ask_shape.output_form == "draft"
        and (
            plan.ask_shape.audience_scope == "internal"
            or plan.outreach_channel == "internal_slack"
        )
        and plan.side_effect_policy == "draft_or_read_only"
        and not plan.requires_approved_context
    )


def reconcile_manual_request_followup(
    plan: ManualRequestPlan,
    *,
    workflow_context: Mapping[str, Any] | None = None,
) -> ManualRequestPlan:
    """Reconcile a typed follow-up delta with one verified provider result set.

    The current semantic plan still owns the requested output. A prior provider
    receipt may preserve collection identity and exact read scope, but never
    grants a write or selects an unrelated route. A requested item projection
    is incompatible with an aggregate-only count operation, so that structural
    contradiction is repaired before the Gmail executor sees the plan.
    """

    trusted_scope = _trusted_prior_provider_result_scope(workflow_context)
    requested_fields = list(plan.gmail_requested_fields)
    warnings = list(plan.planner_warnings)
    updates: dict[str, Any] = {"provider_result_scope": None}

    if requested_fields and plan.provider_result_mode == "count":
        updates["provider_result_mode"] = "items"
        warnings.append(
            "Reconciled aggregate count to item projection because the current turn "
            "requested provider fields that a count cannot supply."
        )

    gmail_scope_matches = bool(
        trusted_scope is not None
        and requested_fields
        and trusted_scope.provider_system == "gmail"
        and plan.target_agent == "gmail_triage"
        and plan.provider_system in {"unspecified", "gmail"}
        and plan.gmail_date_scope
        in {"unspecified", trusted_scope.gmail_date_scope}
        and plan.gmail_mailbox_direction
        in {"unspecified", trusted_scope.gmail_mailbox_direction}
    )
    if gmail_scope_matches:
        assert trusted_scope is not None
        updates.update(
            {
                "provider_system": trusted_scope.provider_system,
                "provider_operations": ["search", "read"],
                "provider_read_scope": trusted_scope.provider_read_scope,
                "provider_result_mode": "items",
                "gmail_mailbox_direction": trusted_scope.gmail_mailbox_direction,
                "gmail_date_scope": trusted_scope.gmail_date_scope,
                "gmail_query": "",
                "target_type": trusted_scope.target_type,
                "side_effect_policy": "draft_or_read_only",
                "provider_result_scope": trusted_scope,
            }
        )
        if trusted_scope.item_count is not None:
            updates["desired_count"] = max(1, min(10, trusted_scope.item_count))
        warnings.append(
            "Preserved the verified prior provider result-set scope while applying "
            "the current typed item-field projection."
        )

    airtable_scope_matches = bool(
        trusted_scope is not None
        and trusted_scope.provider_system == "airtable"
        and trusted_scope.airtable_table
        and plan.target_agent == "airtable_context_agent"
        and plan.provider_system in {"unspecified", "airtable"}
        and plan.provider_result_mode == "items"
        and (
            not plan.primary_target.strip()
            or trusted_scope.airtable_table.casefold() in plan.primary_target.strip().casefold()
        )
    )
    if airtable_scope_matches:
        assert trusted_scope is not None
        updates.update(
            {
                "provider_system": "airtable",
                "provider_operations": ["read"],
                "provider_read_scope": "bounded_collection",
                "provider_result_mode": "items",
                "primary_target": trusted_scope.airtable_table,
                "target_type": "business_system_context",
                "task_objective": "context_lookup",
                "expected_artifact_type": "context_summary",
                "side_effect_policy": "draft_or_read_only",
                "provider_result_scope": trusted_scope,
            }
        )
        if trusted_scope.item_count is not None:
            updates["desired_count"] = max(1, min(10, trusted_scope.item_count))
        warnings.append(
            "Preserved the verified Airtable aggregate scope while applying the "
            "current typed item projection."
        )

    updates["planner_warnings"] = list(dict.fromkeys(warnings))
    return plan.model_copy(update=updates)


def _trusted_prior_provider_result_scope(
    workflow_context: Mapping[str, Any] | None,
) -> ManualProviderResultSetScope | None:
    if not workflow_context:
        return None
    value = workflow_context.get("prior_provider_result_scope")
    if value in (None, "", {}):
        return None
    try:
        scope = ManualProviderResultSetScope.model_validate(value)
    except (TypeError, ValueError):
        return None
    if not scope.verified or not scope.complete:
        return None
    if scope.provider_read_scope != "bounded_collection":
        return None
    return scope


def is_internal_slack_composition_plan(plan: ManualRequestPlan) -> bool:
    """Return whether a typed plan only composes internal Slack copy.

    This deliberately interprets the planner's structured channel, provider,
    context, and side-effect fields. It does not inspect the original request
    or grant permission to post the resulting draft.
    """

    channel = re.sub(r"[\s-]+", "_", str(plan.outreach_channel or "").strip().lower())
    internal_audience = bool(
        plan.ask_shape.audience_scope == "internal"
        or channel
        in {
            "slack",
            "slack_only",
            "slack_thread",
            "team_channel",
            "internal_slack",
            "internal_team_slack",
            "internal_team_channel",
        }
    )
    return bool(
        plan.target_agent == "outreach_composer"
        and internal_audience
        and not plan.recipient
        and plan.intent in {"route_request", "outreach_draft"}
        and not plan.workflow
        and not plan.requires_durable_state
        and not plan.requires_live_search
        and plan.provider_system in {"unspecified", "slack"}
        and not plan.provider_operations
        and plan.side_effect_policy == "draft_or_read_only"
    )


def _explicit_audience_scope(lower: str) -> str:
    """Extract only an explicit audience boundary from the current turn.

    Audience scope can relax an external-outreach prerequisite, but it never
    selects an owner, provider, tool, or write path. Ambiguous audience wording
    remains planner-owned.
    """

    if re.search(
        r"\b(?:internal|our)\s+(?:slack|team(?:\s+(?:slack|channel))?|"
        r"staff(?:\s+channel)?|workspace(?:\s+channel)?)\b"
        r"|\b(?:slack|team)\s+(?:internal\s+)?(?:note|update|sentence)\b"
        r"|\b(?:in|within|keep\s+it\s+in)\s+this\s+(?:slack\s+)?thread\s+only\b",
        lower,
    ):
        return "internal"
    if re.search(
        r"\b(?:external|customer|client|prospect|partner)\s+"
        r"(?:email|message|outreach|copy|audience)\b"
        r"|\b(?:email|linkedin)\s+(?:to|for)\s+(?:the\s+)?"
        r"(?:customer|client|prospect|partner)\b",
        lower,
    ):
        return "external"
    return "unspecified"


def _normalize_llm_plan_contract(candidate: ManualRequestPlan) -> ManualRequestPlan:
    """Resolve schema contradictions from typed intent/provider relationships."""

    mutation_operations = {"create", "update", "delete", "attach"}
    planned_mutations = mutation_operations.intersection(candidate.provider_operations)
    mutation_steps = {
        step.operation
        for step in candidate.provider_action_steps
        if step.operation in mutation_operations
    }
    complete_provider_write_contract = bool(
        candidate.intent == "business_system_write"
        and candidate.task_objective == "business_system_write"
        and candidate.expected_artifact_type == "business_system_write_plan"
        and candidate.provider_system != "unspecified"
        and candidate.side_effect_policy == "internal_write_approval_required"
        and planned_mutations
        and mutation_steps
        and mutation_steps <= planned_mutations
    )
    internal_gmail_collection_draft = bool(
        candidate.provider_system == "gmail"
        and candidate.target_type == "gmail_message_collection"
        and candidate.provider_read_scope == "bounded_collection"
        and candidate.provider_result_mode == "items"
        and candidate.ask_shape.output_form == "draft"
        and candidate.ask_shape.audience_scope == "internal"
        and candidate.provider_operations
        and set(candidate.provider_operations) <= {"read", "search", "verify"}
        and not candidate.recipient
    )
    owner_by_intent: dict[str, ManualTargetAgent] = {
        "company_research": "business_research_analyst",
        "research_brief": "business_research_analyst",
        "opportunity_search": "opportunity_scout",
        "opportunity_to_outreach_loop": "opportunity_scout",
        "gmail_triage": "gmail_triage",
        "outreach_draft": "outreach_composer",
        "slack_operations": "chief_of_staff",
        "browser_diagnostics": "chief_of_staff",
        "reference_capture": "chief_of_staff",
        "continue_work_item": "orchestrator",
        "blocked_send": "clarification",
        "clarification": "clarification",
    }
    owner_by_provider: dict[str, ManualTargetAgent] = {
        "google_calendar": "chief_of_staff",
        "gmail": "gmail_triage",
        "airtable": "airtable_context_agent",
        "google_workspace": "google_workspace_context_agent",
        "zotero": "zotero_context_agent",
        "slack": "chief_of_staff",
    }
    provider_by_owner = {
        owner: provider
        for provider, owner in owner_by_provider.items()
        if owner
        in {
            "gmail_triage",
            "airtable_context_agent",
            "google_workspace_context_agent",
            "zotero_context_agent",
        }
    }
    updates: dict[str, Any] = {}
    warnings = list(candidate.planner_warnings)
    if (
        complete_provider_write_contract
        and candidate.ask_shape.permission_state == "read_only"
    ):
        updates["ask_shape"] = candidate.ask_shape.model_copy(
            update={"permission_state": "approval_required"}
        )
        warnings.append(
            "Reconciled a contradictory read-only permission label with the "
            "planner's complete typed provider-write contract; provider identity, "
            "approval, live-gate, and receipt checks still apply."
        )
    if internal_gmail_collection_draft:
        updates.update(
            {
                "target_agent": "gmail_triage",
                "workflow": ["gmail_triage", "outreach_composer"],
                "intent": "outreach_draft",
                "task_objective": "outreach_draft",
                "expected_artifact_type": "outreach_draft",
                "outreach_channel": candidate.outreach_channel or "internal_slack",
                "requires_approved_context": False,
                "requires_durable_state": True,
            }
        )
        warnings.append(
            "Separated the bounded Gmail candidate read from the requested internal "
            "draft: Gmail Triage selects provider evidence before Outreach Composer "
            "writes one Slack-visible draft."
        )
    expected_owner = owner_by_intent.get(candidate.intent)
    if internal_gmail_collection_draft:
        expected_owner = "gmail_triage"
    if candidate.target_type == "local_document_collection":
        expected_owner = "chief_of_staff"
    if candidate.intent in {"business_system_write", "context_lookup"}:
        if candidate.provider_system == "unspecified":
            inferred_provider = provider_by_owner.get(candidate.target_agent)
            if inferred_provider:
                updates["provider_system"] = inferred_provider
        else:
            expected_owner = owner_by_provider.get(candidate.provider_system)
    if (
        expected_owner is not None
        and candidate.target_agent != expected_owner
        and len(candidate.workflow) <= 1
    ):
        updates["target_agent"] = expected_owner
        warnings.append(
            "Aligned the selected owner with the structured intent/provider "
            "contract; request wording was not reclassified."
        )
    if not updates:
        return candidate
    updates["planner_warnings"] = list(dict.fromkeys(warnings))
    return candidate.model_copy(update=updates)


def _repair_structurally_invalid_llm_plan(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan,
) -> ManualRequestPlan:
    """Repair schema contradictions without restoring keyword route authority."""

    updates: dict[str, Any] = {}
    warnings = list(candidate.planner_warnings)
    ungrounded_clarification = bool(
        (candidate.target_agent == "clarification" or candidate.intent == "clarification")
        and not candidate.missing_required_information
    )
    unowned_route_only = bool(
        candidate.target_agent == "orchestrator"
        and candidate.intent == "route_request"
        and not candidate.workflow
        and not candidate.requires_durable_state
    )
    ownerless_single_owner_execution = bool(
        candidate.target_agent == "orchestrator"
        and candidate.intent != "continue_work_item"
        and not candidate.workflow
        and not candidate.requires_durable_state
        and (
            candidate.provider_system != "unspecified"
            or candidate.provider_operations
            or candidate.task_objective
            not in {"clarification", "route_or_continue"}
        )
    )
    if ungrounded_clarification or unowned_route_only or ownerless_single_owner_execution:
        owner_by_provider: dict[str, ManualTargetAgent] = {
            "google_calendar": "chief_of_staff",
            "gmail": "gmail_triage",
            "airtable": "airtable_context_agent",
            "google_workspace": "google_workspace_context_agent",
            "zotero": "zotero_context_agent",
            "slack": "chief_of_staff",
        }
        explicit_owner = candidate.requested_agent or base.requested_agent
        recovered_provider = candidate.provider_system
        if (
            recovered_provider == "unspecified"
            and ownerless_single_owner_execution
            and explicit_owner not in {None, "orchestrator", "clarification"}
            and base.provider_system != "unspecified"
            and base.target_agent not in {"clarification", "orchestrator"}
        ):
            recovered_provider = base.provider_system
        recovery_owner = owner_by_provider.get(recovered_provider)
        if recovery_owner is None and explicit_owner not in {
            None,
            "orchestrator",
            "clarification",
        }:
            recovery_owner = explicit_owner
        if recovery_owner is None:
            recovery_owner = "chief_of_staff"
        recovered_operations = list(candidate.provider_operations)
        if not recovered_operations and recovered_provider == base.provider_system:
            recovered_operations = list(base.provider_operations)
        operations = set(recovered_operations)
        if operations & {"create", "update", "delete", "attach"}:
            recovered_intent: ManualRequestIntent = "business_system_write"
            recovered_task: ManualTaskObjective = "business_system_write"
            recovered_artifact: ManualExpectedArtifactType = "business_system_write_plan"
        elif recovered_provider != "unspecified" and operations <= {
            "read",
            "search",
            "verify",
        }:
            recovered_intent = "context_lookup"
            recovered_task = "context_lookup"
            recovered_artifact = "context_summary"
        else:
            recovered_intent = "route_request"
            recovered_task = "route_or_continue"
            recovered_artifact = "none"
        if (
            recovered_provider == base.provider_system
            and recovery_owner == base.target_agent
            and base.task_objective
            not in {"clarification", "route_or_continue", "context_lookup"}
            and candidate.task_objective
            in {"clarification", "route_or_continue", "context_lookup"}
        ):
            recovered_intent = base.intent
            recovered_task = base.task_objective
            recovered_artifact = base.expected_artifact_type
        updates.update(
            {
                "target_agent": recovery_owner,
                "workflow": [],
                "intent": recovered_intent,
                "objective": candidate.objective or base.objective,
                "provider_system": recovered_provider,
                "provider_operations": recovered_operations,
                "task_objective": recovered_task,
                "expected_artifact_type": recovered_artifact,
                "requires_durable_state": False,
                "missing_required_information": [],
            }
        )
        warnings.append(
            "Recovered an executable owner from the typed requested-agent/provider "
            "contract because the LLM returned an unexplained clarification or "
            "ownerless route. No keyword route was restored."
        )

    outreach_context_needed = bool(
        candidate.target_agent == "outreach_composer"
        or "outreach_composer" in candidate.workflow
        or candidate.intent in {"outreach_draft", "opportunity_to_outreach_loop"}
    )
    if (
        candidate.requires_approved_context
        and not outreach_context_needed
        and "requires_approved_context" not in updates
    ):
        updates["requires_approved_context"] = False
        warnings.append("Removed an approval-context prerequisite from a non-outreach plan.")

    if len(candidate.workflow) > 1 and not candidate.requires_durable_state:
        updates["requires_durable_state"] = True

    if not updates:
        return candidate
    updates["planner_warnings"] = list(dict.fromkeys(warnings))
    return candidate.model_copy(update=updates)


def _prune_forbidden_llm_capabilities(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan,
) -> ManualRequestPlan:
    """Enforce explicit negative safety boundaries without choosing an owner.

    This validator can remove a forbidden tool or side effect after semantic
    interpretation. It cannot replace the LLM route merely because a phrase
    happened to match a local heuristic, and it must keep the remaining
    provider-free or read-only task executable.
    """

    explicit_constraints = _explicit_negative_constraints(base.objective)
    explicit_constraints.extend(
        str(item or "")
        for item in base.constraints
        if re.match(
            r"^\s*(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b",
            str(item or ""),
            re.I,
        )
    )
    negative_scopes = " ".join(dict.fromkeys(explicit_constraints)).lower()
    if not negative_scopes:
        return candidate

    updates: dict[str, Any] = {}
    warnings = list(candidate.planner_warnings)
    forbidden_provider_access = {
        provider
        for provider in _NEGATIVE_SCOPE_PROVIDER_TERMS
        if any(
            _negative_constraint_forbids_provider_access(item, provider=provider)
            for item in explicit_constraints
        )
    }
    owner_provider = {
        "gmail_triage": "gmail",
        "airtable_context_agent": "airtable",
        "google_workspace_context_agent": "google_workspace",
        "zotero_context_agent": "zotero",
    }
    candidate_owner_provider = owner_provider.get(candidate.target_agent)
    provider_access_forbidden = bool(
        candidate.provider_system in forbidden_provider_access
        or candidate_owner_provider in forbidden_provider_access
    )
    if provider_access_forbidden:
        base_owner_provider = owner_provider.get(base.target_agent)
        recovery_owner = (
            base.target_agent
            if base.target_agent not in {"clarification", "orchestrator"}
            and base_owner_provider not in forbidden_provider_access
            else "chief_of_staff"
        )
        retained_workflow = [
            route
            for route in candidate.workflow
            if owner_provider.get(route) not in forbidden_provider_access
        ]
        updates.update(
            {
                "target_agent": recovery_owner,
                "workflow": retained_workflow if len(retained_workflow) > 1 else [],
                "provider_system": (
                    "unspecified"
                    if candidate.provider_system in forbidden_provider_access
                    else candidate.provider_system
                ),
                "provider_operations": [],
                "provider_action_steps": [],
                "intent": "route_request",
                "task_objective": "route_or_continue",
                "expected_artifact_type": "none",
                "requires_durable_state": bool(
                    base.requires_durable_state or len(retained_workflow) > 1
                ),
                "requires_approved_context": False,
                "gmail_query": "",
                "lookback_days": None,
                "draft_policy": "no_drafts_requested",
                "side_effect_policy": "draft_or_read_only",
            }
        )
        warnings.append(
            "Removed a provider owner because the operator explicitly prohibited "
            "access to that provider; the remaining provider-free task stays executable."
        )
    if candidate.requires_live_search and any(
        _negative_constraint_forbids_live_search(item) for item in explicit_constraints
    ):
        updates["requires_live_search"] = False
        warnings.append(
            "Removed live search because the operator explicitly prohibited it; "
            "the remaining task stays executable."
        )

    write_forbidden = any(
        _negative_constraint_forbids_candidate_write(item, candidate)
        for item in explicit_constraints
    )
    if write_forbidden:
        retained_operations = [
            operation
            for operation in candidate.provider_operations
            if operation in {"read", "search", "verify"}
        ]
        if retained_operations != candidate.provider_operations:
            updates["provider_operations"] = retained_operations
            updates["provider_action_steps"] = [
                step
                for step in candidate.provider_action_steps
                if step.operation in retained_operations
            ]
            warnings.append(
                "Removed provider mutations because the operator explicitly "
                "prohibited writes; read-only work remains available."
            )
        if candidate.intent == "business_system_write":
            provider_read = candidate.provider_system != "unspecified"
            updates.update(
                {
                    "intent": "context_lookup" if provider_read else "route_request",
                    "task_objective": ("context_lookup" if provider_read else "route_or_continue"),
                    "expected_artifact_type": ("context_summary" if provider_read else "none"),
                    "side_effect_policy": "draft_or_read_only",
                }
            )

    draft_forbidden = any(
        _negative_constraint_forbids_composition(item) for item in explicit_constraints
    )
    if (
        draft_forbidden
        and not is_internal_slack_composition_plan(candidate)
        and (
            candidate.target_agent == "outreach_composer"
            or "outreach_composer" in candidate.workflow
            or candidate.intent in {"outreach_draft", "opportunity_to_outreach_loop"}
        )
    ):
        remaining_workflow = [route for route in candidate.workflow if route != "outreach_composer"]
        updates.update(
            {
                "target_agent": (
                    "chief_of_staff"
                    if candidate.target_agent == "outreach_composer"
                    else candidate.target_agent
                ),
                "workflow": remaining_workflow if len(remaining_workflow) > 1 else [],
                "requires_durable_state": bool(
                    base.requires_durable_state or len(remaining_workflow) > 1
                ),
                "intent": "route_request",
                "task_objective": "route_or_continue",
                "expected_artifact_type": "none",
                "requires_approved_context": False,
                "side_effect_policy": "draft_or_read_only",
            }
        )
        warnings.append(
            "Removed outreach drafting because the operator explicitly prohibited "
            "it; the remaining internal task stays executable."
        )

    if not updates:
        return candidate
    updates["planner_warnings"] = list(dict.fromkeys(warnings))
    return candidate.model_copy(update=updates)


def _candidate_uses_explicitly_forbidden_capability(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan,
) -> bool:
    """Reject a model route that contradicts a negative capability boundary."""

    explicit_constraints = _explicit_negative_constraints(base.objective)
    explicit_constraints.extend(
        str(item or "")
        for item in base.constraints
        if re.match(
            r"^\s*(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b",
            str(item or ""),
            re.I,
        )
    )
    negative_scopes = " ".join(dict.fromkeys(explicit_constraints)).lower()
    if not negative_scopes:
        return False
    if (
        not is_internal_slack_composition_plan(candidate)
        and (
            candidate.target_agent == "outreach_composer"
            or "outreach_composer" in candidate.workflow
            or candidate.intent in {"outreach_draft", "opportunity_to_outreach_loop"}
        )
        and any(_negative_constraint_forbids_composition(item) for item in explicit_constraints)
    ):
        return True
    if candidate.intent == "business_system_write" and any(
        _negative_constraint_forbids_candidate_write(item, candidate)
        for item in explicit_constraints
    ):
        return True
    return bool(
        candidate.requires_live_search
        and any(_negative_constraint_forbids_live_search(item) for item in explicit_constraints)
    )


def _candidate_turns_constraint_into_prerequisite_or_blocker(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan,
) -> bool:
    """Keep feasible positive work executable after forbidden capabilities are pruned."""

    explicit_constraints = _explicit_negative_constraints(base.objective)
    explicit_constraints.extend(
        str(item or "")
        for item in base.constraints
        if re.match(
            r"^\s*(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b",
            str(item or ""),
            re.I,
        )
    )
    negative_scopes = " ".join(dict.fromkeys(explicit_constraints)).lower()
    if (
        not negative_scopes
        or base.target_agent == "clarification"
        or base.intent in {"clarification", "blocked_send"}
    ):
        return False
    if candidate.target_agent == "clarification" or candidate.intent == "clarification":
        return True
    if _candidate_resolves_supplied_context_with_chief(base, candidate):
        return False
    if (
        not base.requires_approved_context
        and candidate.requires_approved_context
        and base.intent != "business_system_write"
        and re.search(
            r"\b(?:draft|send|post|publish|share|write|create|modify|update|delete)\b",
            negative_scopes,
        )
    ):
        return True
    bounded_response = bool(
        base.target_agent == "chief_of_staff"
        and not base.workflow
        and (
            _looks_like_supplied_context_synthesis_request(base.objective)
            or _looks_like_prior_response_transformation_request(base.objective)
        )
    )
    return bool(
        bounded_response
        and (
            candidate.target_agent != "chief_of_staff"
            or bool(candidate.workflow)
            or candidate.intent not in {"route_request", "slack_operations"}
        )
    )


def _candidate_resolves_supplied_context_with_chief(
    base: ManualRequestPlan,
    candidate: ManualRequestPlan,
) -> bool:
    """Allow semantic planning to collapse a heuristic graph for one bounded answer."""

    return bool(
        base.target_agent == "chief_of_staff"
        and _looks_like_supplied_context_synthesis_request(base.objective)
        and not looks_like_stateful_work_request(base.objective)
        and not provider_tool_action_bound(base.objective)
        and candidate.target_agent == "chief_of_staff"
        and candidate.intent in {"route_request", "slack_operations"}
        and not candidate.workflow
        and not candidate.requires_live_search
        and candidate.side_effect_policy == "draft_or_read_only"
    )


def _semantic_target_agent(
    request: str | dict[str, Any] | None,
    text: str,
    *,
    requested_agent: ManualTargetAgent | None,
    honor_text_agent_mentions: bool = True,
) -> ManualTargetAgent:
    route_text = _without_negated_route_action_clauses(text)
    lower = route_text.lower()
    explicit_text_agent = _direct_agent_prefix_agent(text)
    conversational_agent = _conversational_named_agent(text)
    if (
        honor_text_agent_mentions
        and requested_agent in {None, "orchestrator"}
        and explicit_text_agent not in {None, "orchestrator"}
    ):
        if (
            explicit_text_agent
            in {"chief_of_staff", "opportunity_scout", "business_research_analyst"}
            and _looks_like_anchor_competitor_research_request(route_text)
        ):
            return "business_research_analyst"
        return explicit_text_agent
    if (
        honor_text_agent_mentions
        and requested_agent in {None, "orchestrator"}
        and conversational_agent not in {None, "orchestrator"}
    ):
        if (
            conversational_agent
            in {"chief_of_staff", "opportunity_scout", "business_research_analyst"}
            and _looks_like_anchor_competitor_research_request(route_text)
        ):
            return "business_research_analyst"
        return conversational_agent
    if _looks_like_browser_diagnostics_only_request(text):
        return (
            requested_agent
            if requested_agent in {"chief_of_staff", "orchestrator"}
            else "chief_of_staff"
        )
    if requested_agent in {None, "orchestrator", "chief_of_staff"} and is_calendar_action_candidate(
        route_text
    ):
        # Calendar action admission is a provider-bound routing hint. It keeps
        # a complete Calendar mutation with the Chief control plane before the
        # generic company/research and side-effect fallbacks inspect its title.
        return "chief_of_staff"
    if _looks_like_research_table_synthesis(route_text):
        return "business_research_analyst"
    if _looks_like_anchor_competitor_research_request(route_text):
        return "business_research_analyst"
    if _looks_like_unnamed_company_set_discovery(route_text):
        return "opportunity_scout"
    if (
        requested_agent in {None, "orchestrator", "chief_of_staff"}
        and has_explicit_local_attachment_context(route_text)
        and _local_attachment_company_target(route_text)
        and _looks_like_source_summary_request(lower)
    ):
        # A selected source packet about a named company is bounded Business
        # Research evidence. Chief may still be the human-facing front door,
        # but the source-summary specialist owns interpretation.
        return "business_research_analyst"
    if requested_agent in {
        None,
        "orchestrator",
        "chief_of_staff",
    } and _looks_like_supplied_context_synthesis_request(route_text):
        return "chief_of_staff"
    if requested_agent in {
        None,
        "orchestrator",
        "chief_of_staff",
    } and _looks_like_prior_response_transformation_request(route_text):
        return "chief_of_staff"
    if requested_agent in {
        None,
        "orchestrator",
        "chief_of_staff",
    } and _looks_like_internal_handoff_request(route_text):
        return "chief_of_staff"
    if requested_agent == "chief_of_staff" and _request_forbids_provider_access(
        text,
        provider="gmail",
    ):
        # Inline email text can be interpreted directly. A provider name in the
        # task does not authorize that provider when the operator explicitly
        # says not to use it.
        return "chief_of_staff"
    if requested_agent in {
        None,
        "orchestrator",
        "chief_of_staff",
    } and _looks_like_internal_gmail_contact_lookup_request(route_text):
        # A known relationship in the operator's own mailbox is a bounded
        # Gmail evidence question, not public contact discovery. Chief remains
        # the requested front door while Gmail Triage owns the provider read
        # and evidence-backed specialist answer.
        return "gmail_triage"
    if requested_agent in {
        None,
        "orchestrator",
        "chief_of_staff",
    } and looks_like_gmail_collection_read(
        route_text
    ) and not _looks_like_orchestrator_owned_workflow(route_text):
        # A broad mailbox read is still single-owner provider work. Chief stays
        # the operator-facing front door while Gmail Triage owns interpretation.
        return "gmail_triage"
    if requested_agent == "chief_of_staff" and is_single_owner_gmail_reply_request(route_text):
        # Chief remains the operator-facing manager, while the one specialist
        # that can read the selected thread and synthesize reply copy owns
        # execution.
        return "gmail_triage"
    if requested_agent not in {None, "orchestrator"}:
        task_text = _strip_direct_agent_prefix(route_text)
        if _has_clear_task_ownership(requested_agent, request, task_text):
            return requested_agent
        inferred_owner = _semantic_target_agent(
            request,
            task_text,
            requested_agent=None,
            honor_text_agent_mentions=False,
        )
        if inferred_owner not in {
            requested_agent,
            "clarification",
            "orchestrator",
        } and _owner_reassignment_supported(
            requested_agent=requested_agent,
            candidate_agent=inferred_owner,
            request=request,
            text=task_text,
            candidate_intent=_intent_for_target(
                inferred_owner,
                task_text,
                workflow_allowed=False,
            ),
        ):
            # Explicit specialist mentions are routing advice, not ownership.
            # Re-evaluate the task without that mention and delegate only when
            # the ordinary semantic planner identifies another concrete owner.
            # Ambiguous and continuation-only asks remain with the requested
            # specialist instead of being silently rerouted.
            return inferred_owner
    delegated_context_target = _business_context_target_agent(route_text)
    if (
        requested_agent not in {None, "orchestrator", "chief_of_staff"}
        and delegated_context_target
        and _looks_like_explicit_business_context_operation(route_text)
    ):
        return delegated_context_target
    if (
        requested_agent == "chief_of_staff"
        and delegated_context_target in _MUTABLE_CONTEXT_AGENT_TARGETS
        and _looks_like_internal_business_system_mutation(route_text)
    ):
        return delegated_context_target
    if requested_agent == "chief_of_staff" and _looks_like_finance_expense_receipt_write(
        route_text
    ):
        # Receipt-backed expense writes belong to Airtable's schema/tool owner.
        # Chief of Staff remains the requested control-plane agent, but the
        # direct specialist must perform the bounded provider operation.
        return "airtable_context_agent"
    if requested_agent and requested_agent != "orchestrator":
        return requested_agent
    if _looks_like_underspecified_modify_request(route_text):
        return "clarification"
    if _looks_like_eval_scorecard_review(lower):
        return "chief_of_staff"
    if _looks_like_finance_expense_receipt_write(text):
        return "chief_of_staff"
    if _looks_like_reference_capture_request(lower):
        return "chief_of_staff"
    if _looks_like_contact_discovery_request(route_text):
        return "business_research_analyst"
    if _looks_like_explicit_business_research_instruction(text):
        return "business_research_analyst"
    if looks_like_opportunity_to_outreach_loop(route_text):
        return "opportunity_scout"
    context_target_agent = delegated_context_target
    if context_target_agent == "zotero_context_agent" and _looks_like_zotero_context_request(
        route_text
    ):
        return context_target_agent
    if looks_like_zotero_article_request(text) or looks_like_zotero_collection_request(text):
        return "business_research_analyst"
    if context_target_agent is not None:
        return context_target_agent
    if _looks_like_finance_operations_request(route_text):
        return "chief_of_staff"
    if _looks_like_research_table_synthesis(route_text):
        return "business_research_analyst"
    if _looks_like_chief_of_staff_operational_request(lower):
        return "chief_of_staff"
    if _looks_like_gmail_label_request(lower):
        return "gmail_triage"
    if _looks_like_gmail_style_request(lower):
        return "gmail_triage"
    if _looks_like_outreach_variant_request(lower):
        return "outreach_composer"
    if (
        looks_like_send_side_effect(text)
        and _looks_like_direct_outreach_send_request(lower)
        and not _looks_like_discovery_outreach_workflow(route_text)
    ):
        return "outreach_composer"
    if looks_like_send_side_effect(text) and not _looks_like_discovery_outreach_workflow(
        route_text
    ):
        return "clarification"
    if _company_comparison_target(text):
        return "business_research_analyst"
    if looks_like_resume_request(text):
        return "orchestrator"
    if _looks_like_orchestrator_owned_workflow(route_text):
        return _workflow_start_agent(route_text)
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
        if _looks_like_discovery_outreach_workflow(route_text):
            return "opportunity_scout"
        return "outreach_composer"
    if OPPORTUNITY_RE.search(lower):
        return "opportunity_scout"
    if _looks_like_discovery_outreach_workflow(route_text):
        return "opportunity_scout"
    if looks_like_company(text):
        return "business_research_analyst"
    return "clarification"


def _looks_like_supplied_context_synthesis_request(text: str) -> bool:
    """Keep bounded transformations of supplied facts with the operator manager.

    A factual phrase such as ``provider write`` must not be interpreted as the
    command ``write a note`` merely because both words occur in the same
    continuation envelope.
    """

    normalized = " ".join(str(text or "").split())
    lower = normalized.lower()
    supplied_context = _has_supplied_context_boundary(
        lower
    ) or _looks_like_inline_fact_packet_synthesis(
        normalized
    ) or has_explicit_local_attachment_context(normalized)
    if not supplied_context:
        return False
    explicit_source_operation = provider_tool_action_bound(text)
    # An explicit "use only these supplied facts" boundary is already stronger
    # evidence than a response-verb list. Keep the owning model responsible for
    # interpreting assess/decide/prepare and equivalent wording while Python
    # continues to block an actual provider operation.
    return not explicit_source_operation


def has_explicit_local_attachment_context(text: str) -> bool:
    """Return whether the current turn supplies a bounded local PDF/image.

    The Slack bridge appends a trusted marker after it downloads attachment
    bytes. Direct CLI tests may instead name the attached artifact naturally.
    A path alone is not enough: requiring either that marker or attachment
    language avoids treating an incidental filename in prose as selected
    evidence.
    """

    source = str(text or "")
    if not _LOCAL_ARTIFACT_PATH_RE.search(source):
        return False
    return bool(
        has_materialized_slack_attachment_context(source)
        or _LOCAL_ATTACHMENT_CONTEXT_RE.search(positive_capability_text(source))
    )


def has_materialized_slack_attachment_context(text: str) -> bool:
    """Return whether the Slack bridge supplied a local attachment reference."""

    source = str(text or "")
    return bool(
        _LOCAL_ARTIFACT_PATH_RE.search(source)
        and _SLACK_ATTACHMENT_PATH_MARKER in " ".join(source.lower().split())
    )


def _looks_like_inline_fact_packet_synthesis(text: str) -> bool:
    """Recognize a self-contained factual premise without requiring magic wording.

    Natural operator asks often state the relevant fact directly and then say not
    to search. Requiring an additional ``use only this note`` clause makes those
    asks unnecessarily brittle. The no-search boundary, an asserted fact, and a
    requested synthesis must all be present before the inline text is treated as
    selected context.
    """

    normalized = " ".join(str(text or "").split())
    if not normalized or not request_forbids_live_research(normalized):
        return False
    coordination_text = positive_capability_text(normalized)
    if re.search(
        r"\b(?:hand\s*off|handoff|work\s*item[- ]capable|"
        r"best next (?:owner|agent|specialist))\b",
        coordination_text,
        re.I,
    ):
        return False
    if not re.search(
        r"\b(?:assess|evaluate|summari[sz]e|explain|compare|map|identify|"
        r"choose|decide|recommend|prepare|draft|give|return|write|turn)\b",
        normalized,
        re.I,
    ):
        return False
    asserted_fact = re.search(
        r"(?:^|[.!?;:]\s+)"
        r"(?:"
        r"[A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,5}"
        r"|(?:the\s+)?(?:company|vendor|organization|team|product|service|"
        r"project|candidate|client)"
        r")\s+"
        r"(?:is|are|has|have|sells|offers|provides|uses|tracks|says|reports|"
        r"supports|serves|builds|develops|operates)\b",
        normalized,
    )
    return asserted_fact is not None


def _has_supplied_context_boundary(text: str) -> bool:
    lower = " ".join(str(text or "").lower().split())
    return bool(
        re.search(
            r"\b(?:using|use|from|within|based on|grounded in)\s+only\s+"
            r"(?:this|the|these|those|provided|supplied|operator[- ]supplied)\s+"
            r"(?:(?:approved|provided|supplied|operator[- ]supplied)\s+)?"
            r"(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+)?"
            r"(?:note|notes|fact|facts|text|material|information|details|context)\b"
            r"|\b(?:using|use|from|within|based on|grounded in)\s+"
            r"(?:this|the|these|those|provided|supplied|operator[- ]supplied)\s+"
            r"(?:(?:approved|provided|supplied|operator[- ]supplied)\s+)?"
            r"(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+)?"
            r"(?:note|notes|fact|facts|text|material|information|details|context)"
            r"\s+only\b"
            r"|\bstay within (?:the )?(?:supplied|provided|following) text\b"
            r"|\bturn (?:the )?following (?:supplied |provided )?"
            r"(?:note|notes|fact|facts|text|material|information|details|context)\b",
            lower,
        )
    )


def looks_like_supplied_context_synthesis_request(text: str) -> bool:
    """Expose the shared supplied-context route contract to agent executors."""

    return _looks_like_supplied_context_synthesis_request(text)


def looks_like_stateful_work_request(text: str) -> bool:
    """Recognize explicit durable/resumable work authority, not incidental task words."""

    lower = " ".join(str(text or "").lower().split())
    return bool(
        re.search(
            r"\btrack\s+(?:this|the)\s+"
            r"(?:review|assessment|work|request|task)\b",
            lower,
        )
        or re.search(
            r"\b(?:resumable|resume\s+later|pick\s+(?:it|this)\s+up\s+later|"
            r"work\s*item)\b"
            r"|\b(?:track|preserve|save)\b[^.;\n]{0,120}"
            r"\b(?:state|review|assessment|artifacts?|results?|recommendation)\b"
            r"[^.;\n]{0,120}\b(?:later|resume|revise|continue|return\s+to)\b",
            lower,
        )
    )


def _looks_like_prior_response_transformation_request(text: str) -> bool:
    """Keep bounded follow-up formatting/correction work with the operator manager."""

    route_text = _without_negated_route_action_clauses(text)
    lower = " ".join(str(route_text or "").lower().split())
    prior_response = bool(
        re.search(
            r"\b(?:same|original|prior|previous|earlier|last|above)\b"
            r"[^.;\n]{0,100}\b(?:request|answer|response|reply|result|output|"
            r"note|summary|bullets?|points?)\b"
            r"|\b(?:request|answer|response|reply|result|output|note|summary)\b"
            r"[^.;\n]{0,100}\b(?:above|again|before|earlier)\b",
            lower,
        )
    )
    transformation = bool(
        re.search(
            r"\b(?:correct|fix|finish|give|keep|make|reformat|return|rewrite|"
            r"shorten|condense|reply)\b",
            lower,
        )
    )
    response_artifact = bool(
        re.search(
            r"\b(?:answer|bullets?|introduction|heading|note|points?|reply|"
            r"response|summary|wording)\b",
            lower,
        )
    )
    positive_specialist_operation = bool(
        _business_context_target_agent(route_text)
        or OUTREACH_RE.search(lower)
        or OPPORTUNITY_RE.search(lower)
        or _looks_like_explicit_business_research_instruction(route_text)
        or looks_like_email(None, route_text)
    )
    return (
        prior_response
        and transformation
        and response_artifact
        and not positive_specialist_operation
    )


def _looks_like_internal_handoff_request(text: str) -> bool:
    """Recognize a request to choose or describe an owner without executing it."""

    lower = " ".join(positive_capability_text(text).lower().split())
    handoff_output = bool(
        re.search(
            r"\b(?:internal\s+handoff|best\s+next\s+(?:owner|agent|specialist)|"
            r"(?:concise|brief)\s+handoff|"
            r"next\s+owner\s+or\s+agent|recommended\s+next\s+path|"
            r"what\s+remains\s+blocked|information\s+.+?\s+before\s+committing)\b",
            lower,
        )
    )
    management_action = bool(
        re.search(
            r"\b(?:return|recommend|identify|choose|select|name|explain|review)\b",
            lower,
        )
    )
    direct_execution = bool(
        re.search(
            r"\b(?:run|execute|have|ask|call|delegate|hand\s+off\s+to)\b"
            r"[^.;\n]{0,100}\b(?:agent|specialist|research|scout|triage)\b",
            lower,
        )
    )
    return handoff_output and management_action and not direct_execution


def _has_clear_task_ownership(
    agent: ManualTargetAgent,
    request: str | dict[str, Any] | None,
    text: str,
) -> bool:
    """Return whether the task itself clearly supports one specialist owner.

    This is deliberately stricter than ordinary route inference. It is used
    only to decide whether an explicit but incompatible specialist mention may
    be handed off, so ambiguous or multi-owner asks stay with the named agent.
    """

    route_text = _without_negated_route_action_clauses(text)
    lower = " ".join(route_text.lower().split())
    context_owner = _business_context_target_agent(route_text)
    if agent in _CONTEXT_AGENT_TARGETS:
        # Direct-agent prefixes are removed before ordinary task inference so
        # the mention itself cannot become generic routing authority. For a
        # context specialist, however, that prefix also names the provider
        # boundary. Recover the original operator text only to prove the narrow
        # combination of the same provider owner plus a bounded provider
        # operation. This retains ``Zotero Context Agent, read ...`` while still
        # allowing ``Zotero Context Agent, research this on the web`` to
        # delegate because the latter does not request a Zotero tool action.
        original_route_text = _without_negated_route_action_clauses(
            payload_text(request)
        )
        original_context_owner = _business_context_target_agent(
            original_route_text
        )
        return bool(
            context_owner == agent and _looks_like_explicit_business_context_operation(route_text)
            or (
                original_context_owner == agent
                and _looks_like_explicit_business_context_operation(
                    original_route_text
                )
            )
            or _named_context_agent_task_is_bounded(agent, route_text)
        )
    if agent == "gmail_triage":
        return bool(
            looks_like_email(request, route_text)
            or _looks_like_gmail_label_request(lower)
            or _looks_like_gmail_style_request(lower)
        )
    if agent == "outreach_composer":
        draft_or_reply = bool(
            re.search(r"\b(?:draft|write|compose|prepare)\b", lower)
            and re.search(
                r"\b(?:outreach|email|linkedin|message|note|reply|response)\b",
                lower,
            )
        )
        approved_draft = bool(
            draft_or_reply
            and re.search(
                r"\b(?:approved (?:inline )?(?:context|facts?)|draft-only)\b",
                lower,
            )
        )
        return bool(
            approved_draft
            or (
                not _looks_like_discovery_outreach_workflow(route_text)
                and (
                    OUTREACH_RE.search(lower)
                    or _looks_like_outreach_variant_request(lower)
                    or draft_or_reply
                )
            )
        )
    if agent == "opportunity_scout":
        if _looks_like_anchor_competitor_research_request(route_text):
            return False
        return bool(
            _looks_like_unnamed_company_set_discovery(route_text)
            or looks_like_opportunity_to_outreach_loop(route_text)
            or OPPORTUNITY_RE.search(lower)
            or (
                _looks_like_actionable_opportunity_request(lower)
                and re.search(
                    r"\b(?:find|identify|search|scout|source|discover|list|"
                    r"evaluate|assess|review|qualify)\b",
                    lower,
                )
            )
        )
    if agent == "business_research_analyst":
        return bool(
            _looks_like_anchor_competitor_research_request(route_text)
            or _company_profile_target(route_text)
            or _company_comparison_target(route_text)
            or _workflow_company_target(route_text)
            or _looks_like_research_table_synthesis(route_text)
            or _looks_like_explicit_business_research_instruction(route_text)
        )
    if agent == "chief_of_staff":
        coordinated_research = bool(
            re.search(r"\b(?:research|source-backed|evidence)\b", lower)
            and _looks_like_actionable_opportunity_request(lower)
            and re.search(r"\b(?:approval|checkpoint|draft-only|do not send)\b", lower)
        )
        gmail_to_draft_workflow = bool(
            looks_like_email(request, route_text)
            and re.search(
                r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,120}"
                r"\b(?:reply|response|email|message)\b",
                lower,
            )
            and not is_single_owner_gmail_reply_request(route_text)
        )
        return bool(
            _looks_like_browser_diagnostics_only_request(route_text)
            or _looks_like_chief_of_staff_operational_request(lower)
            or (
                _looks_like_finance_operations_request(route_text)
                and _business_context_target_agent(route_text) is None
            )
            or chief_advisory_coordination_requested(route_text)
            or _looks_like_reference_capture_request(lower)
            or coordinated_research
            or gmail_to_draft_workflow
        )
    return False


def is_single_owner_gmail_reply_request(text: str) -> bool:
    """Return whether Gmail alone can read the mailbox and draft response copy.

    This is an ownership threshold, not a phrase-specific execution lane. The
    request must explicitly keep the draft in the current Slack thread and must
    not ask another specialist or context provider to enrich the answer.
    """

    raw = str(text or "")
    actionable = " ".join(positive_capability_text(raw).lower().split())
    if not actionable or not looks_like_thread_local_draft_request(raw):
        return False
    if not looks_like_email(None, actionable):
        return False
    if not re.search(r"\b(?:draft|compose|prepare|reply|respond)\b", actionable):
        return False
    additional_owner = bool(
        re.search(
            r"\b(?:research|source[- ]backed|public sources?|web search|internet|"
            r"opportunit(?:y|ies)|airtable|google (?:drive|docs?|workspace)|"
            r"zotero|local (?:files?|documents?)|kni (?:docs?|documents?|context)|"
            r"approved kni context|cv|resume)\b",
            actionable,
        )
    )
    return not additional_owner


def _owner_reassignment_supported(
    *,
    requested_agent: ManualTargetAgent | None,
    candidate_agent: ManualTargetAgent,
    request: str | dict[str, Any] | None,
    text: str,
    candidate_intent: ManualRequestIntent,
    workflow: list[ManualTargetAgent] | None = None,
) -> bool:
    """Require capability-bearing evidence before changing an explicit owner.

    Semantic planning may suggest an owner, but generic operational terms,
    time-pressure context, and provider names in supplied facts are not routing
    authority. A reassignment needs both a capability-specific plan intent and
    bounded positive evidence in the operator request.
    """

    if requested_agent in {None, "orchestrator"}:
        return True
    if candidate_agent == requested_agent:
        return True

    route_text = _without_negated_route_action_clauses(text)
    if candidate_agent == "chief_of_staff":
        return bool(
            (workflow and len(workflow) > 1)
            or (
                candidate_intent == "browser_diagnostics"
                and _looks_like_browser_diagnostics_only_request(route_text)
            )
            or (
                candidate_intent == "business_system_write"
                and is_calendar_action_candidate(route_text)
            )
            or (
                candidate_intent == "reference_capture"
                and _looks_like_reference_capture_request(route_text.lower())
            )
            or (
                candidate_intent == "slack_operations"
                and _looks_like_slack_operations_request(route_text)
            )
            or (
                candidate_intent == "blocked_send"
                and _looks_like_slack_operations_request(route_text)
                and _looks_like_blocked_side_effect_request(route_text)
            )
            or _looks_like_finance_operations_request(route_text)
            or _looks_like_internal_handoff_request(route_text)
            or chief_advisory_coordination_requested(route_text)
        )

    supported_intents: dict[ManualTargetAgent, frozenset[ManualRequestIntent]] = {
        "gmail_triage": frozenset({"gmail_triage"}),
        "business_research_analyst": frozenset({"company_research", "research_brief"}),
        "opportunity_scout": frozenset({"opportunity_search", "opportunity_to_outreach_loop"}),
        "outreach_composer": frozenset({"outreach_draft", "blocked_send"}),
        "airtable_context_agent": frozenset({"context_lookup", "business_system_write"}),
        "google_workspace_context_agent": frozenset({"context_lookup", "business_system_write"}),
        "zotero_context_agent": frozenset({"context_lookup", "business_system_write"}),
        "rss_context_agent": frozenset({"context_lookup"}),
        "preprints_context_agent": frozenset({"context_lookup"}),
    }
    return bool(
        candidate_intent in supported_intents.get(candidate_agent, frozenset())
        and _has_clear_task_ownership(candidate_agent, request, route_text)
    )


def resolve_manual_request_owner(
    requested_agent: str | None,
    plan: ManualRequestPlan,
    *,
    request_text: str | None = None,
) -> ManualTargetAgent:
    """Resolve one owner without reclassifying an LLM plan from request words.

    An explicit agent mention remains routing advice. When live semantic
    planning succeeded, its owner is authoritative and Python only validates
    whether that owner exists and may perform the scoped operation. Heuristic
    evidence reconciliation remains a planner-unavailable fallback.
    """

    requested = normalize_manual_agent(requested_agent)
    candidate = normalize_manual_agent(str(plan.target_agent or ""))
    if ExecutionIntentAuthority.from_value(plan).canonical:
        return candidate or requested or "orchestrator"
    if requested in {None, "orchestrator"}:
        return candidate or requested or "orchestrator"
    if candidate in {None, "orchestrator", "clarification"}:
        return requested
    if candidate == requested:
        return requested
    evidence_text = str(request_text if request_text is not None else plan.objective)
    if requested == "chief_of_staff" and chief_advisory_coordination_requested(evidence_text):
        return requested
    if _owner_reassignment_supported(
        requested_agent=requested,
        candidate_agent=candidate,
        request=evidence_text,
        text=evidence_text,
        candidate_intent=plan.intent,
        workflow=plan.workflow,
    ):
        return candidate
    return requested


def chief_advisory_coordination_requested(text: str) -> bool:
    """Return whether Chief should coordinate named specialists as advisors.

    This is an ownership guard, not an intent classifier. It preserves an
    explicit Chief-of-Staff coordination ask when the requested context agents
    are advisory inputs rather than the direct execution surface.
    """

    route_text = _without_negated_route_action_clauses(str(text or ""))
    lower = " ".join(route_text.lower().split())
    has_advisory_scope = bool(
        re.search(
            r"\b(?:advisory\s+(?:specialists?|context)|"
            r"read[- ]only\s+advisors?|as\s+(?:an?\s+)?advisor)\b",
            lower,
        )
    )
    has_coordination_action = bool(
        re.search(
            r"\b(?:coordinate|use|plan|design|decide|identify|summarize|"
            r"recommend|propose|return)\b",
            lower,
        )
    )
    has_named_context_specialist = bool(_business_context_target_agent(route_text))
    return has_advisory_scope and has_coordination_action and has_named_context_specialist


def _looks_like_underspecified_modify_request(text: str) -> bool:
    """Block pronoun-only mutations when no selected object context is attached."""

    normalized = " ".join(str(text or "").lower().split()).strip(" .?!")
    return bool(
        re.fullmatch(
            r"(?:please\s+)?(?:update|edit|modify|change|revise|delete|remove)\s+"
            r"(?:it|this|that|the\s+(?:item|record|draft|event|file|document|row|note))",
            normalized,
        )
    )


def _intent_for_target(
    target_agent: ManualTargetAgent,
    text: str,
    *,
    workflow_allowed: bool = True,
) -> ManualRequestIntent:
    lower = _without_negated_route_action_clauses(text).lower()
    if (
        target_agent == "business_research_analyst"
        and has_explicit_local_attachment_context(text)
        and _local_attachment_company_target(text)
        and _looks_like_source_summary_request(lower)
    ):
        return "company_research"
    if _looks_like_supplied_context_synthesis_request(text):
        return "route_request"
    if _looks_like_browser_diagnostics_only_request(text):
        return "browser_diagnostics"
    if target_agent == "chief_of_staff" and is_calendar_action_candidate(text):
        return "business_system_write"
    if target_agent in {"airtable_context_agent", "chief_of_staff"} and (
        _looks_like_finance_expense_receipt_write(text)
    ):
        return "business_system_write"
    if target_agent == "chief_of_staff" and _looks_like_internal_business_system_mutation(text):
        return "business_system_write"
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(lower):
        return "reference_capture"
    if (
        workflow_allowed
        and target_agent == "opportunity_scout"
        and looks_like_opportunity_to_outreach_loop(text)
    ):
        return "opportunity_to_outreach_loop"
    if (
        target_agent in _MUTABLE_CONTEXT_AGENT_TARGETS
        and _looks_like_internal_business_system_mutation(text)
    ):
        return "business_system_write"
    if _looks_like_blocked_side_effect_request(
        text
    ) and not _looks_like_discovery_outreach_workflow(text):
        return "blocked_send"
    if target_agent == "chief_of_staff" and _looks_like_slack_operations_request(lower):
        return "slack_operations"
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


def _looks_like_contact_discovery_request(text: str) -> bool:
    lower = " ".join(_without_negated_route_action_clauses(str(text or "")).lower().split())
    has_discovery = bool(
        re.search(r"\b(?:find|identify|locate|research|source|look\s+up)\b", lower)
    )
    has_contact = bool(
        re.search(
            r"\b(?:contact|decision[- ]maker|partnerships?\s+lead|"
            r"partner\s+lead|commercial\s+lead)\b",
            lower,
        )
    )
    has_drafting = bool(
        re.search(
            r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,80}\b(?:email|message|outreach)\b", lower
        )
    )
    return has_discovery and has_contact and not has_drafting


def _looks_like_internal_gmail_contact_lookup_request(text: str) -> bool:
    """Recognize a known-contact question that should use bounded Gmail evidence.

    This is intentionally about the relationship shape, not a company or
    provider name. Public prospecting remains Business Research work.
    """

    source = _without_negated_route_action_clauses(str(text or ""))
    lower = " ".join(source.lower().split())
    if _request_forbids_provider_access(source, provider="gmail"):
        return False
    if re.search(
        r"\b(?:public|website|web|linkedin|decision[- ]maker|sales\s+lead|"
        r"partnerships?\s+lead|business\s+development\s+lead)\b",
        lower,
    ):
        return False
    if re.search(
        r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,100}\b"
        r"(?:email|message|outreach|reply)\b",
        lower,
    ):
        return False
    return looks_like_known_contact_relationship(lower)


def _internal_gmail_contact_lookup_entity(text: str) -> str:
    """Extract a distinctive organization/account term for a Gmail query."""

    source = _strip_direct_agent_prefix(str(text or ""))
    return extract_known_contact_entity(source)


def _looks_like_slack_operations_request(lower: str) -> bool:
    normalized = " ".join(positive_capability_text(lower).lower().split())
    if "slack ops" in normalized or "slack operations" in normalized:
        return True
    action = (
        r"(?:audit|review|inspect|diagnose|debug|evaluate|assess|recommend|"
        r"summari[sz]e|check|route|post|send|update|edit|delete|remove|read|"
        r"list|find|reply|continue|resolve)"
    )
    slack_object = (
        r"(?:slack\s+(?:channel|message|thread|workflow|workflow\s+status|"
        r"route|routing|socket|post|follow-up|follow\s+up)|"
        r"selected\s+slack(?:\s+message)?|ai-agents-workflow|#[a-z0-9_-]+)"
    )
    same_clause = r"[^.!?;\n]{0,140}"
    return bool(
        re.search(rf"\b{action}\b{same_clause}\b{slack_object}\b", normalized, re.I)
        or re.search(
            rf"\b{slack_object}\b{same_clause}\b{action}\b",
            normalized,
            re.I,
        )
        or re.search(
            rf"\b(?:post|send|publish|reply|update|edit|delete|remove)\b"
            rf"{same_clause}\bslack\b",
            normalized,
            re.I,
        )
    )


def _looks_like_finance_expense_receipt_write(text: str) -> bool:
    lower = " ".join(str(text or "").lower().split())
    if not _FINANCE_EXPENSE_RECEIPT_WRITE_RE.search(str(text or "")):
        return False
    has_receipt_marker = any(marker in lower for marker in ("receipt", "invoice"))
    has_local_artifact = bool(_LOCAL_ARTIFACT_PATH_RE.search(str(text or "")))
    if not has_receipt_marker and not has_local_artifact:
        return False
    return bool(re.search(r"\b(?:add|create|insert|record|update|change|set|fill)\b", lower))


def _looks_like_finance_operations_request(text: str) -> bool:
    """Recognize bounded finance aggregation or review as Chief-owned work."""

    lower = " ".join(positive_capability_text(text).lower().split())
    if not lower:
        return False
    finance_metric = bool(
        re.search(
            r"\b(?:income|expenses?|spend|deductions?|tax(?:es)?|"
            r"total\s+expenses|additional\s+taxes)\b",
            lower,
        )
    )
    finance_operation = bool(
        re.search(
            r"\b(?:total|sum|calculate|summari[sz]e|review|inspect|find|"
            r"compare|reconcile|current\s+quarter)\b",
            lower,
        )
    )
    period_or_tracker = bool(
        re.search(r"\b(?:q[1-4]|quarter(?:\s+[1-4])?|20\d{2})\b", lower)
        or re.search(
            r"\b(?:finance(?:_tax_|\s+tax\s+|\s+)tracker|financial\s+tracker|"
            r"airtable\s+tracker)\b",
            lower,
        )
    )
    research_request = bool(
        re.search(
            r"\b(?:company|companies|market|industry|competitor|vendor)\b",
            lower,
        )
        and re.search(r"\b(?:research|profile|source[- ]backed|web)\b", lower)
    )
    return finance_metric and finance_operation and period_or_tracker and not research_request


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
        "workflow bridge",
        "orchestrator bridge",
        "slack bridge",
        "workitem bridge",
        "work item bridge",
        "slack workflow",
        "workflow status",
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


def _looks_like_gmail_style_request(lower: str) -> bool:
    """Recognize sent-mail style learning as a Gmail-owned capability."""

    has_mail_context = bool(
        re.search(r"\b(?:gmail|emails?|sent\s+(?:mail|emails?|messages?))\b", lower)
    )
    has_style_intent = bool(
        re.search(
            r"\b(?:style|tone|voice|wording|phrasing|write\s+like|similar\s+style|mimic)\b",
            lower,
        )
    )
    return has_mail_context and has_style_intent


def _business_context_target_agent(text: str) -> ManualTargetAgent | None:
    """Resolve explicit business-system context targets without phrase-specific lanes."""

    lower = " ".join(str(text or "").lower().split())
    if not lower:
        return None
    if "airtable" in lower:
        return "airtable_context_agent"
    if re.search(
        r"\b(?:google\s+workspace|google\s+drive|google\s+docs?|google\s+sheets?|"
        r"google\s+slides?|powerpoint|pptx|slide\s+deck|presentation\s+deck|"
        r"gdrive|drive\s+folder|workspace\s+(?:doc|sheet|slide|deck|artifact|folder)|kniops)\b",
        lower,
    ):
        return "google_workspace_context_agent"
    if re.search(r"\bzotero\b", lower):
        return "zotero_context_agent"
    if re.search(
        r"\b(?:rss(?:\s+context|\s+feed|\s+announcements?)?|announcement\s+feed|"
        r"announcements?\s+context|#announcements)\b",
        lower,
    ):
        return "rss_context_agent"
    if re.search(
        r"\b(?:preprints?(?:\s+context|\s+history)?|preprint\s+history|"
        r"#knowledge[- ]hub|knowledge\s+hub\s+preprints?)\b",
        lower,
    ):
        return "preprints_context_agent"
    return None


def _looks_like_explicit_business_context_operation(text: str) -> bool:
    """Recognize bounded read/write verbs aimed at an explicitly named source system."""

    normalized = " ".join(str(text or "").lower().split())
    return bool(
        _business_context_target_agent(normalized) and provider_tool_action_bound(normalized)
    )


def _named_context_agent_task_is_bounded(
    agent: ManualTargetAgent,
    text: str,
) -> bool:
    """Validate a provider-local task after an adapter removes its agent prefix.

    The captured named agent supplies the provider boundary; the remaining text
    must still contain both a concrete provider operation and a resource shape
    owned by that agent. Explicitly naming another provider prevents retention.
    """

    actionable = " ".join(positive_capability_text(text).lower().split())
    if not actionable:
        return False
    if re.search(
        r"\b(?:web|internet|online|gmail|e-?mail|inbox|airtable|calendar|"
        r"google\s+(?:calendar|drive|docs?|sheets?|workspace)|zotero|"
        r"rss(?:\s+feed)?|announcement\s+feed|preprints?(?:\s+context)?|"
        r"local\s+(?:file|document)|repository|repo|website|url|https?://)\b",
        actionable,
        re.I,
    ):
        return False
    if not re.search(
        r"\b(?:read|search|look\s+up|find|list|inspect|review|check|query|open|"
        r"fetch|get|create|add|update|change|modify|edit|delete|remove|trash|"
        r"upload|attach|save)\b",
        actionable,
        re.I,
    ):
        return False
    resource_patterns: dict[ManualTargetAgent, str] = {
        "airtable_context_agent": (
            r"\b(?:base|table|record|row|field|view|expense|income|airtable)\b"
        ),
        "google_workspace_context_agent": (
            r"\b(?:document|doc|folder|sheet|spreadsheet|slide|deck|file|workspace)\b"
        ),
        "zotero_context_agent": (
            r"\b(?:abstract|article|journal|paper|item|library|collection|citation|"
            r"author|metadata|note|tag|attachment)\b"
        ),
        "rss_context_agent": r"\b(?:feed|announcement|entry|headline|item)\b",
        "preprints_context_agent": (
            r"\b(?:preprint|abstract|paper|article|study|trial|history|item)\b"
        ),
    }
    pattern = resource_patterns.get(agent)
    return bool(pattern and re.search(pattern, actionable, re.I))


def _looks_like_zotero_context_request(text: str) -> bool:
    lower = " ".join(str(text or "").lower().split())
    return bool(
        "zotero context" in lower
        or (
            "zotero" in lower
            and _provider_selection_rank(lower, zotero_context=True) is not None
            and re.search(
                r"\b(?:article|paper|item|record|journal|library)\b",
                lower,
            )
        )
        or re.search(r"\bzotero\s+context\s+agent\b", lower)
        or re.search(r"\buse\s+zotero\b[^.\n;]{0,80}\bas\s+context\b", lower)
        or ("zotero" in lower and re.search(r"\bitem\s+keys?\b", lower))
        or (
            "zotero" in lower
            and re.search(
                r"\b(?:create|add|write|update|edit|modify|revise|rename|delete|remove|"
                r"clean\s*up|verify)\b[^.\n;]{0,120}"
                r"\b(?:notes?|tags?|collections?|item\s+metadata)\b",
                lower,
            )
        )
        or re.search(
            r"\buse\s+the\s+zotero\s+(?:article|paper|item|source|study|trial)\b"
            r"[^.\n;]{0,160}\bbefore\b",
            lower,
        )
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
    if looks_like_thread_local_draft_request(text):
        return False
    if (
        re.search(r"\bdraft\b[\s\S]{0,120}\b(?:response|reply|email|message|outreach)\b", lower)
        and re.search(r"\b(?:after|with|pending)\s+(?:human\s+)?approval\b", lower)
        and not re.search(r"\b(?:send|post|publish|share|deliver)\s+(?:it|the|this)?\b", lower)
    ):
        return False
    direct_slack_post = bool(
        re.search(
            r"^\s*(?:please\s+)?(?:post|publish|send|share)\b"
            r"[^.!?;\n]{0,160}\b(?:slack|#[a-z0-9_-]+)\b",
            lower,
        )
        and not re.search(r"\b(?:gmail|email|inbox)\b", lower)
    )
    return (
        looks_like_send_side_effect(text)
        or direct_slack_post
        or _looks_like_external_write_side_effect(text)
    )


def _looks_like_external_write_side_effect(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    lower = re.sub(r"\bprior\s+post\b", "prior message", lower)
    if re.search(
        r"\b(?:do\s+not|don't|dont|never|no)\b[\s\S]{0,180}"
        r"\b(?:add|attach|change|create|delete|deliver|edit|export|insert|modify|"
        r"move|publish|remove|rename|reschedule|save|schedule|send|share|shift|"
        r"update|write)\b",
        lower,
    ):
        return False
    write_verb = (
        r"(?:add|attach|change|create|delete|deliver|edit|export|insert|modify|"
        r"move|publish|remove|rename|reschedule|save|schedule|send|share|shift|"
        r"update|write)"
    )
    if "zotero" in lower and re.search(
        r"\b(?:create|update|delete|modify|write|remove|revise)\b[\s\S]{0,100}\bnotes?\b"
        r"|\bnotes?\b[\s\S]{0,100}\b(?:create|update|delete|modify|write|remove|revise)\b",
        lower,
    ):
        return True
    target_object = (
        r"(?:record|row|table|tracker|field|file|doc|document|sheet|folder|attachment|"
        r"airtable|drive|workspace|gmail\s+draft|slack|crm|calendar|meeting|event|"
        r"collection|item|library|slide|deck|presentation)"
    )
    return bool(
        re.search(rf"\b{write_verb}\b[^.!?;\n]{{0,100}}\b{target_object}\b", lower)
        or re.search(rf"\b{target_object}\b[^.!?;\n]{{0,100}}\b{write_verb}\b", lower)
        or re.search(
            r"\bschedule\b[^.!?;\n]{0,80}\b(?:meeting|event|call|follow-up|follow up)\b",
            lower,
        )
    )


def _looks_like_internal_business_system_mutation(text: str) -> bool:
    if _looks_like_write_plan_only_request(text):
        return False
    actionable_text = _without_negated_route_action_clauses(str(text or ""))
    actionable_text = re.sub(
        r"(?:^|(?<=[.!?;]))\s*(?:do\s+not|don't|dont|never)\b[^.!?;]*[.!?;]?",
        " ",
        actionable_text,
        flags=re.I,
    )
    actionable_text = re.sub(
        r"\b(?:but|and)\s+(?:do\s+not|don't|dont|never)\b[^.!?;]*",
        " ",
        actionable_text,
        flags=re.I,
    )
    actionable_text = re.sub(
        r"\b(?:draft|write|compose|prepare)\b[\s\S]{0,120}\b"
        r"(?:internal\s+)?slack\s+(?:update|message|copy|brief|note)\b",
        "draft internal copy",
        actionable_text,
        flags=re.I,
    )
    lower = " ".join(actionable_text.lower().split())
    if re.search(r"\b(?:send|post|publish|share|deliver)\b", lower):
        return False
    return _looks_like_external_write_side_effect(actionable_text)


def _looks_like_write_plan_only_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    asks_for_plan = bool(
        re.search(
            r"\b(?:save|write|create|creation|update|delete|migration)\s+plan\b"
            r"|\bplan\s+(?:for|to)\s+(?:save|write|create|update|delete|move)\b",
            normalized,
        )
    )
    requests_execution = bool(
        re.search(
            r"\b(?:execute|perform|apply|carry\s+out|go\s+ahead\s+and|"
            r"create\s+it\s+now|do\s+it\s+now)\b",
            normalized,
        )
    )
    return asks_for_plan and not requests_execution


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


def _desired_count_is_domain_limit(text: str) -> bool:
    """Distinguish an explicit domain-result limit from response formatting."""

    if _COUNT_RE.search(text) is None and _COUNT_WORD_RE.search(text) is None:
        return False
    if re.search(
        r"\b(?:bullets?|talking\s+points?|sentences?|paragraphs?|words?)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return True


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
        profile_target = _company_profile_target(cleaned)
        if profile_target:
            return profile_target
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
        return _first_nonempty(
            (
                _internal_gmail_contact_lookup_entity(cleaned)
                if _looks_like_internal_gmail_contact_lookup_request(cleaned)
                else ""
            ),
            _quoted_text(cleaned),
            _subject_text(cleaned),
            cleaned[:120],
        )
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
    if target_agent == "gmail_triage" and (
        _looks_like_internal_gmail_contact_lookup_request(text)
        or looks_like_gmail_collection_read(text)
    ):
        return "gmail_message_collection"
    if target_agent == "chief_of_staff" and looks_like_local_kni_evidence_lookup(text):
        return "local_document_collection"
    if _looks_like_supplied_context_synthesis_request(text) and not (
        target_agent == "business_research_analyst" and looks_like_company(text)
    ):
        return "unknown"
    if _looks_like_browser_diagnostics_request(text):
        return "url"
    if target_agent == "chief_of_staff" and is_calendar_action_candidate(text):
        return "business_system_context"
    if target_agent == "chief_of_staff" and _looks_like_finance_expense_receipt_write(text):
        return "business_system_context"
    if target_agent == "chief_of_staff" and _looks_like_reference_capture_request(lower):
        return "operator_reference"
    if target_agent == "chief_of_staff" and looks_like_company(text):
        return "company"
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
    if intent == "business_system_write":
        return "business_system_write"
    if intent == "context_lookup":
        return "context_lookup"
    if target_agent == "gmail_triage" and _looks_like_internal_gmail_contact_lookup_request(text):
        return "contact_discovery"
    if intent == "gmail_triage":
        return "gmail_triage"
    if intent == "outreach_draft":
        return "outreach_draft"
    if intent == "slack_operations":
        return "slack_operations"
    if target_agent == "business_research_analyst" and _looks_like_contact_discovery_request(text):
        return "contact_discovery"
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
    if objective == "business_system_write":
        return "business_system_write_plan"
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


def _local_attachment_company_target(text: str) -> str:
    """Extract a company explicitly bound to the selected local attachment."""

    source = str(text or "")
    attachment = (
        r"(?i:source\s+packet|research\s+packet|research\s+brief|company\s+brief|"
        r"company\s+profile|file|image|pdf|document)"
    )
    company = (
        r"(?P<name>(?:[A-Z][\w&.-]*|[A-Z]{2,})"
        r"(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    )
    for pattern in (
        re.compile(rf"\b{attachment}\s+(?i:for|about|on)\s+{company}\b"),
        re.compile(rf"\b(?i:attached)\s+{company}\s+{attachment}\b"),
        re.compile(rf"\b(?i:for)\s+{company}\b"),
    ):
        match = pattern.search(source)
        if match is None:
            continue
        candidate = _clean_company_candidate(match.group("name"))
        if candidate and _plausible_company_profile_target(candidate):
            return candidate
    return ""


def _looks_like_actionable_opportunity_request(lower: str) -> bool:
    return any(
        marker in lower
        for marker in (
            "opportunity",
            "opportunities",
            "funding",
            "grant",
            "grants",
            "partnership",
            "partnerships",
            "pilot",
            "pilots",
            "collaboration",
            "collaborations",
            "advisory",
            "consulting",
            "contract",
            "contracts",
            "rfp",
            "rfi",
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
    company_profile_target = _company_profile_target(cleaned)
    if company_profile_target:
        entities.append(company_profile_target)
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
    lower = _without_negated_route_action_clauses(text).lower()
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


def _company_profile_target(text: str) -> str:
    """Extract the exact company named by a bounded identity/profile ask."""

    cleaned = _strip_direct_agent_prefix(str(text or ""))
    for pattern in _COMPANY_PROFILE_TARGET_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        candidate = _clean_company_candidate(match.group("name"))
        if (
            candidate
            and _plausible_company_profile_target(candidate)
            and candidate.lower()
            not in {
                "company",
                "the company",
                "this company",
                "it",
                "this",
                "that",
                "them",
                "they",
                "their",
            }
        ):
            return candidate
    return ""


def _plausible_company_profile_target(candidate: str) -> bool:
    """Reject prose fragments captured from broad workflow instructions."""

    words = [word.strip("()[]{}.,:;") for word in candidate.split() if word.strip()]
    if not 1 <= len(words) <= 6:
        return False
    if len(words) == 1:
        return bool(re.search(r"[A-Za-z0-9]", words[0]))
    joiners = {"and", "of", "the", "&"}
    return all(word.lower() in joiners or word[:1].isupper() for word in words)


def _company_comparison_target(text: str) -> str:
    match = _COMPANY_COMPARISON_RE.search(str(text or ""))
    if match:
        company_a = _clean_company_candidate(match.group("company_a"))
        company_b = _clean_company_candidate(match.group("company_b"))
        if company_a and company_b:
            return f"{company_a} vs {company_b}"
    list_match = _COMPANY_LIST_COMPARISON_RE.search(str(text or ""))
    if not list_match:
        return ""
    companies = [
        _clean_company_candidate(item)
        for item in re.split(
            r"\s*,\s*and\s+|\s*,\s*|\s+and\s+",
            list_match.group("companies"),
        )
    ]
    cleaned = [company for company in companies if company]
    return " vs ".join(cleaned) if len(cleaned) >= 2 else ""


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


def _looks_like_anchor_competitor_research_request(text: str) -> bool:
    """Recognize one named research anchor plus a separate competitor set."""

    lower = " ".join(str(text or "").lower().split())
    anchor_scope = bool(
        re.search(
            r"\b(?:as|is)\s+(?:the|an?)\s+anchor\b"
            r"|\banchor\s+(?:company|target|product|platform)\b",
            lower,
        )
    )
    competitor_scope = bool(
        re.search(
            r"\b(?:competitors?|competing\s+(?:companies|products?|platforms?|tools?)|"
            r"alternatives?|comparables?)\b",
            lower,
        )
    )
    research_scope = bool(
        re.search(
            r"\b(?:research|characterize|profile|analy[sz]e|investigate|assess|"
            r"evaluate|compare|identify|find)\b",
            lower,
        )
    )
    return bool(anchor_scope and competitor_scope and research_scope)


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
    if _looks_like_comparison_format_constraint(lower):
        constraints.append("comparison-format")
    if re.search(r"\banswer\b", lower) and re.search(r"\bsynthesis\b", lower):
        constraints.append("answer-and-synthesis")
    constraints.extend(_exclusion_constraints(text))
    constraints.extend(_explicit_negative_constraints(text))
    return constraints


def _looks_like_comparison_format_constraint(lower: str) -> bool:
    """Return true for requested comparison output, not test/control wording."""

    if re.search(
        r"\b(?:comparison|compare)\s+(?:smoke|test|run|control|variant|variants)\b", lower
    ):
        return False
    if re.search(r"\b(?:table|matrix|side[- ]by[- ]side)\b", lower):
        return True
    if re.search(r"\bcompare\s+(?!notes\b)", lower):
        return True
    return bool(
        re.search(r"\bcomparison\b", lower)
        and re.search(r"\b(?:format|table|matrix|between|of|for)\b", lower)
    )


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


def _explicit_negative_constraints(text: str) -> list[str]:
    """Preserve operator restrictions verbatim without using them for routing."""

    source = _separate_mixed_negative_positive_clauses(
        _without_reported_quoted_text(text)
    )
    constraints: list[str] = []
    for match in _LEADING_NEGATED_CAPABILITY_PREFIX_RE.finditer(source):
        cleaned = " ".join(match.group(0).split()).strip(" ,:;-")
        if cleaned:
            constraints.append(cleaned.lower())
    source = _LEADING_NEGATED_CAPABILITY_PREFIX_RE.sub(" ", source)
    for match in re.finditer(
        r"\b(?P<constraint>(?:do\s+not|don't|dont|never|without|no)\b"
        r"[^.;!?\n]{0,240})",
        source,
        flags=re.I,
    ):
        cleaned = " ".join(match.group("constraint").split()).strip(" ,:;-")
        if cleaned:
            constraints.append(cleaned.lower())
    return list(dict.fromkeys(constraints))


def _without_reported_quoted_text(text: str) -> str:
    """Remove quoted source/example prose before inferring operator prohibitions."""

    source = str(text or "")
    patterns = (
        r'"[^"\n]{0,600}"',
        r"“[^”\n]{0,600}”",
        r"‘[^’\n]{0,600}’",
        r"(?<!\w)'[^'\n]{0,600}'(?!\w)",
    )
    for pattern in patterns:
        source = re.sub(pattern, " ", source)
    return source


_NEGATIVE_SCOPE_PROVIDER_TERMS: dict[str, tuple[str, ...]] = {
    "google_calendar": ("calendar", "event", "schedule"),
    "gmail": ("gmail", "email", "draft", "label", "archive", "mailbox"),
    "airtable": ("airtable", "record", "row", "table", "base", "expense"),
    "google_workspace": (
        "google workspace",
        "google drive",
        "google doc",
        "document",
        "file",
        "folder",
        "sheet",
    ),
    "zotero": ("zotero", "note", "item", "collection"),
    "slack": ("slack", "channel", "thread", "post"),
}


def _negative_constraint_provider_scopes(constraint: str) -> set[str]:
    normalized = " ".join(str(constraint or "").lower().split())
    return {
        provider
        for provider, terms in _NEGATIVE_SCOPE_PROVIDER_TERMS.items()
        if any(re.search(rf"\b{re.escape(term)}s?\b", normalized) for term in terms)
    }


def _negative_constraint_forbids_provider_access(
    constraint: str,
    *,
    provider: str,
) -> bool:
    """Return whether one negative clause forbids reading or using a provider.

    Mutation-only boundaries such as ``do not create a Gmail draft`` do not
    disable a permitted provider read. This gate is deliberately narrower than
    provider write enforcement.
    """

    normalized = " ".join(str(constraint or "").lower().split())
    if (
        re.search(
            r"\b(?:use|access|read|check|query|search|browse|fetch|retrieve|open|connect\s+to)\b",
            normalized,
        )
        is None
    ):
        return False
    if re.search(
        r"\b(?:any\s+providers?|provider\s+(?:tools?|systems?)|external\s+"
        r"(?:tools?|providers?|systems?)|tools?\s+or\s+providers?|"
        r"providers?\s+or\s+tools?)\b",
        normalized,
    ):
        return True
    access_provider_terms: dict[str, tuple[str, ...]] = {
        "google_calendar": ("google calendar", "calendar"),
        "gmail": ("gmail", "mailbox", "inbox"),
        "airtable": ("airtable",),
        "google_workspace": (
            "google workspace",
            "google drive",
            "google docs",
            "google sheets",
        ),
        "zotero": ("zotero",),
        "slack": ("slack",),
    }
    provider_terms = access_provider_terms.get(provider, ())
    provider_pattern = "|".join(re.escape(term) for term in provider_terms)
    if provider_pattern and re.search(
        r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
        r"[^.;\n]{0,120}\b(?:use|access|read|check|query|search|browse|fetch|"
        r"retrieve|open|connect\s+to)\b"
        r"[^.;\n]{0,80}\b(?:outside|beyond)\b"
        rf"[^.;\n]{{0,40}}\b(?:{provider_pattern})\b",
        normalized,
    ):
        # "Do not search outside Google Drive" confines the allowed read to
        # that provider; it does not prohibit the provider itself.
        return False
    return any(
        re.search(rf"\b{re.escape(term)}\b", normalized)
        for term in provider_terms
    )


def _request_forbids_provider_access(text: str, *, provider: str) -> bool:
    return any(
        _negative_constraint_forbids_provider_access(item, provider=provider)
        for item in _explicit_negative_constraints(_without_reported_quoted_text(text))
    )


def _negative_constraint_forbids_candidate_write(
    constraint: str,
    candidate: ManualRequestPlan,
) -> bool:
    normalized = " ".join(str(constraint or "").lower().split())
    if not re.search(
        r"\b(?:add|append|attach|change|create|delete|edit|modify|remove|save|"
        r"update|write|make\s+changes?)\b",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:any(?:thing|\s+provider)|provider\s+(?:records?|systems?)|"
        r"external\s+(?:records?|systems?)|externally)\b",
        normalized,
    ):
        return True
    provider_scopes = _negative_constraint_provider_scopes(normalized)
    if provider_scopes:
        return candidate.provider_system in provider_scopes
    return True


def _negative_constraint_forbids_composition(constraint: str) -> bool:
    """Return whether a negative clause actually forbids writing the response.

    A provider-artifact boundary such as ``do not create a Gmail draft`` or
    ``no provider draft`` must not cancel a simultaneously requested,
    provider-free composition in Slack. Deterministic pruning acts only on a
    direct prohibition of drafting/writing the substantive copy; ambiguous
    artifact language remains part of the canonical plan for the model to
    interpret.
    """

    normalized = " ".join(str(constraint or "").lower().split())
    direct = re.search(
        r"\b(?:do\s+not|don't|dont|never|without|avoid|skip)\s+"
        r"(?:an?\s+)?"
        r"(?P<action>draft|drafting|compose|composing|prepare|preparing|"
        r"write|writing)\b(?P<scope>[^,;.]*)",
        normalized,
    )
    if direct is not None:
        scope = direct.group("scope").strip()
        if re.match(
            r"^(?:to|in|into|on)\s+(?:gmail|a\s+provider|the\s+provider|"
            r"airtable|google\s+(?:docs?|drive|workspace)|slack)\b",
            scope,
        ):
            return False
        return True
    if re.search(
        r"\b(?:do\s+not|don't|dont|never|without|avoid|skip)\b"
        r"[^.;!?]{0,180}\b(?:drafting|composing|preparing|writing)\b",
        normalized,
    ):
        return True
    if re.search(
        r"\b(?:do\s+not|don't|dont|never|without|avoid|skip)\b"
        r"[^.;!?]{0,180}\b(?:draft|compose|prepare|write)\s+"
        r"(?:anything|copy|outreach|an?\s+(?:email|reply|response|message|note)|"
        r"the\s+(?:email|reply|response|message|note))\b",
        normalized,
    ):
        return True
    return bool(
        re.search(
            r"\bno\s+(?:draft|drafting|composition|composing|written\s+"
            r"(?:reply|response)|outreach\s+copy|email\s+copy|reply)\b",
            normalized,
        )
    )


def request_forbids_response_composition(text: str) -> bool:
    """Return whether the current ask directly prohibits composing response copy.

    Provider boundaries such as no-send, no-post, and no-provider-draft remain
    safety constraints but do not cancel a simultaneously requested local draft.
    """

    return any(
        _negative_constraint_forbids_composition(constraint)
        for constraint in _explicit_negative_constraints(text)
    )


def _negative_constraint_forbids_live_search(constraint: str) -> bool:
    normalized = " ".join(str(constraint or "").lower().split())
    if re.search(r"\b(?:external\s+tools?|browser\s+automation)\b", normalized):
        return True
    if not re.search(r"\b(?:browse|research|search)\b", normalized):
        return False
    if re.search(
        r"\b(?:web|internet|external|live|browser|online|externally)\b",
        normalized,
    ):
        return True
    if re.search(
        r"\b(?:do\s+not|don't|dont|never|no|without|avoid|skip)\s+"
        r"(?:search|browse|research)\b(?:\s*[,;]|$)",
        normalized,
    ):
        return True
    action_match = re.search(
        r"\b(?:search|browse|research)\b(?P<scope>[^,;.]*)",
        normalized,
    )
    scoped_text = action_match.group("scope") if action_match is not None else normalized
    provider_scopes = _negative_constraint_provider_scopes(scoped_text)
    return not bool(provider_scopes)


def looks_like_opportunity_to_outreach_loop(text: str) -> bool:
    """Return whether text asks for the integrated opportunity -> outreach workflow."""

    raw_text = _without_negated_route_action_clauses(text)
    if _NO_OUTREACH_DRAFT_RE.search(raw_text):
        return False
    return bool(_OPPORTUNITY_TO_OUTREACH_RE.search(raw_text))


def _natural_opportunity_to_outreach_workflow(
    text: str,
) -> list[ManualTargetAgent]:
    """Compile separate natural deliverables into the durable workflow.

    The explicit legacy ``run ... opportunity-to-outreach loop`` command keeps
    its compatibility controller. Natural asks that name distinct discovery,
    research, provider-plan, and drafting work use the WorkItem graph.
    """

    actionable = _without_negated_route_action_clauses(text)
    if not looks_like_opportunity_to_outreach_loop(actionable):
        return []
    if re.search(
        r"\b(?:run|start|execute|do)\b[^.;\n]{0,50}"
        r"\bopportunit(?:y|ies)\s*(?:-|to\s+)?outreach\b[^.;\n]{0,30}"
        r"\b(?:loop|workflow|process)\b",
        actionable,
        re.I,
    ):
        return []
    patterns: tuple[tuple[ManualTargetAgent, re.Pattern[str]], ...] = (
        (
            "opportunity_scout",
            re.compile(
                r"\b(?:find|identify|source|select|choose|rank|recommend)\b"
                r"[^.;\n]{0,120}\bopportunit(?:y|ies)\b",
                re.I,
            ),
        ),
        (
            "business_research_analyst",
            re.compile(r"\b(?:research|investigate|validate)\b", re.I),
        ),
        (
            "airtable_context_agent",
            re.compile(
                r"\b(?:create|prepare|draft|make)\b[^.;\n]{0,100}\bairtable\b"
                r"[^.;\n]{0,100}\b(?:record|write)\s+plan\b"
                r"|\bairtable\b[^.;\n]{0,100}\b(?:record|write)\s+plan\b",
                re.I,
            ),
        ),
        (
            "outreach_composer",
            re.compile(
                r"\b(?:draft|compose|prepare|write)\b[^.;\n]{0,120}\boutreach\b",
                re.I,
            ),
        ),
    )
    ordered = [
        (match.start(), route)
        for route, pattern in patterns
        if (match := pattern.search(actionable)) is not None
    ]
    workflow = list(dict.fromkeys(route for _, route in sorted(ordered)))
    if {
        "opportunity_scout",
        "outreach_composer",
    }.issubset(workflow) and len(workflow) >= 3:
        return workflow
    return []


def _looks_like_discovery_outreach_workflow(text: str) -> bool:
    return bool(_DISCOVERY_OUTREACH_WORKFLOW_RE.search(_without_negated_route_action_clauses(text)))


def _looks_like_explicit_business_research_instruction(text: str) -> bool:
    return bool(_EXPLICIT_BUSINESS_RESEARCH_INSTRUCTION_RE.search(str(text or "")))


def positive_capability_text(text: str) -> str:
    """Return only request text that may provide positive execution evidence.

    Negative capability clauses remain on the canonical request and in the
    parsed constraints. They are removed only from the derived text used for
    owner, tool, stage, and backend selection. A contrasted positive clause
    after ``but``, ``however``, or ``instead`` remains eligible.
    """

    separated_clauses = _separate_mixed_negative_positive_clauses(text)
    without_reporting_examples = _CONDITIONAL_AGENT_REPORTING_CLAUSE_RE.sub(
        " ",
        separated_clauses,
    )
    without_leading_boundaries = _LEADING_NEGATED_CAPABILITY_PREFIX_RE.sub(
        " ",
        without_reporting_examples,
    )
    without_capability_boundaries = _NEGATED_CAPABILITY_CLAUSE_RE.sub(
        " ",
        without_leading_boundaries,
    )
    without_route_actions = _NEGATED_ROUTE_ACTION_CLAUSE_RE.sub(
        " ",
        without_capability_boundaries,
    )
    return _NEGATED_BUSINESS_SYSTEM_ACTION_CLAUSE_RE.sub(" ", without_route_actions)


def _separate_mixed_negative_positive_clauses(text: str) -> str:
    """Keep a positive instruction that follows a bounded negative clause.

    Natural requests often combine a filter or safety boundary with the desired
    action in one sentence, for example, ``skip threads I answered, and draft a
    response here``. The negative clause remains available to safety parsing,
    while the positive clause remains eligible for semantic planning.
    """

    return re.sub(
        r"(?P<negative>\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
        r"(?:(?![.;\n]).){0,220}?),\s*and\s+"
        r"(?=(?:then\s+|please\s+)?"
        r"(?:answer|use|draft|compose|write|prepare|return|give|provide|"
        r"summari[sz]e|review|assess|explain|reformat|list|read|query|"
        r"find|select|choose|create|add|schedule|verify|check|confirm|"
        r"update|modify|edit|delete|remove)\b)",
        r"\g<negative>. ",
        str(text or ""),
        flags=re.I,
    )


def provider_tool_action_bound(text: str) -> bool:
    """Return whether a positive action is bound to a provider/system object.

    Provider names inside supplied facts, examples, negative constraints, or
    architecture commentary are not tool-admission authority.
    """

    actionable = " ".join(positive_capability_text(text).lower().split())
    action = (
        r"(?:read|search|look\s+up|find|list|inspect|review|summari[sz]e|check|"
        r"query|open|fetch|get|create|add|update|change|modify|edit|delete|remove|"
        r"trash|send|post|publish|draft|compose|write|schedule|reschedule|upload|"
        r"attach|save|share|use)"
    )
    provider = (
        r"(?:web|internet|online|slack|gmail|e-?mail|inbox|airtable|calendar|"
        r"google\s+(?:calendar|drive|docs?|sheets?|workspace)|zotero|"
        r"rss(?:\s+feed)?|announcement\s+feed|preprints?(?:\s+context)?|"
        r"local\s+(?:file|document)|repository|repo|website|url|https?://)"
    )
    same_clause = r"[^.!?;\n]{0,140}"
    if _has_supplied_context_boundary(actionable):
        # A provider operation described inside supplied facts is evidence to
        # summarize, not an instruction to call that provider. Within this
        # bounded shape, require a direct imperative before the provider name.
        direct_action = (
            r"(?:read|search|look\s+up|find|list|inspect|check|query|open|fetch|get|"
            r"create|add|update|change|modify|edit|delete|remove|trash|send|post|"
            r"publish|draft|compose|write|schedule|reschedule|upload|attach|save|share)"
        )
        return bool(
            re.search(
                rf"\b{direct_action}\b{same_clause}\b{provider}\b",
                actionable,
                re.I,
            )
        )
    return bool(
        re.search(rf"\b{action}\b{same_clause}\b{provider}\b", actionable, re.I)
        or re.search(rf"\b{provider}\b{same_clause}\b{action}\b", actionable, re.I)
    )


def _without_negated_route_action_clauses(text: str) -> str:
    return positive_capability_text(text)


def _strip_direct_agent_prefix(text: str) -> str:
    cleaned = _strip_kni_direct_prefix(text)
    for alias in sorted(_AGENT_ALIASES, key=len, reverse=True):
        match = re.match(
            rf"^{re.escape(alias)}(?:\s*[:;,.-]\s*|\s+)(?P<body>.*)$",
            cleaned,
            flags=re.I,
        )
        if match:
            return _strip_wrapping_quotes(match.group("body").strip())
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
            rf".{{0,80}}\b(?:the\s+)?{re.escape(alias)}\b"
            rf"|\bact\s+as\s+(?:my\s+|the\s+)?{re.escape(alias)}\b",
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
    best_match = re.search(
        r"\b(?P<topic>(?:best|top|strongest|highest[- ]value)\s+opportunity)\b",
        cleaned,
        re.I,
    )
    match = re.search(r"\bfor\s+(?P<topic>.+)$", cleaned, re.I)
    if best_match is not None:
        candidate = best_match.group("topic").strip()
    elif match:
        candidate = match.group("topic").strip()
        if re.match(
            r"^(?:review|approval|human\s+review|internal\s+review|later)\b",
            candidate,
            re.I,
        ):
            candidate = ""
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
    if re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", text, re.I):
        return 1
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
    if request_forbids_response_composition(text):
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
    if re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", text, re.I):
        today = datetime.now(ZoneInfo("America/New_York")).date()
        parts.append(f"after:{today.strftime('%Y/%m/%d')}")
    else:
        days = _lookback_days(text)
        if days is not None:
            parts.append(f"newer_than:{days}d")
    sender_hint = _gmail_sender_hint(text)
    if sender_hint:
        parts.append(f'"{sender_hint}"')
    elif _looks_like_internal_gmail_contact_lookup_request(text):
        entity = _internal_gmail_contact_lookup_entity(text)
        if entity:
            parts.append(f'"{entity}"')
    return " ".join(parts)


def _gmail_sender_hint(text: str) -> str:
    match = re.search(
        r"\b(?:email|message|thread)s?\s+from\s+"
        r"(?P<sender>[A-Za-z0-9][A-Za-z0-9&.' -]{1,80}?)"
        r"(?=\s+(?:and|that|about|with|then|to)\b|[,.?]|$)",
        text,
        re.I,
    )
    if not match:
        return ""
    sender = " ".join(match.group("sender").split()).strip()
    if re.fullmatch(
        r"(?:today|yesterday|the\s+(?:last|past)\s+\d+\s+days?)",
        sender,
        re.I,
    ):
        return ""
    return sender


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
