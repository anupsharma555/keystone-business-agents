"""Reusable prompt builders for common Slack-entered Keystone requests."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from keystone_agents.schemas.work_item import WorkItemRoute

SLACK_QUERY_PROMPT_SCHEMA = "keystone.slack.query_prompt.v1"
SLACK_QUERY_PROMPT_VERSION = "2026-06-11.v1"

_MAX_RAW_REQUEST_CHARS = 1200
_MAX_CONTEXT_SNIPPET_CHARS = 900
_MAX_PRIOR_RUNS = 3
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b", flags=re.I),
    re.compile(r"\b(?:api[_ -]?key|token|secret)\s*[:=]\s*[^\s,;]+", flags=re.I),
)
_SEND_ACTION_RE = re.compile(
    r"\b(send|post|publish|schedule|submit|create\s+draft|create\s+gmail\s+draft|write\s+files?)\b",
    flags=re.I,
)
_NEGATED_SIDE_EFFECT_WINDOW_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|without|no)\b.{0,220}",
    flags=re.I | re.S,
)
_DRAFT_SAFE_RE = re.compile(r"\b(draft|compose|write|revise|rewrite|suggest)\b", flags=re.I)


class SlackQueryPromptKind(StrEnum):
    """Reusable Slack query prompt families."""

    RESEARCH_SUMMARY = "research_summary"
    DEEPER_RESEARCH = "deeper_research"
    OPPORTUNITY_SEARCH = "opportunity_search"
    THREAD_SUMMARY_NEXT_ACTION = "thread_summary_next_action"
    OUTREACH_DRAFT = "outreach_draft"
    CONTINUE_OR_REVISE = "continue_or_revise"


class SlackQueryPromptInput(BaseModel):
    """Bounded input used to select and render a reusable Slack prompt."""

    raw_request: str = ""
    selected_message_text: str = ""
    selected_message_permalink: str = ""
    channel_name: str = ""
    thread_summary: str = ""
    prior_agent_summaries: list[str] = Field(default_factory=list)
    manual_plan: dict[str, Any] | None = None
    work_item_id: str = ""
    intent: str = ""
    target_route: WorkItemRoute | None = None
    feedback: str = ""

    @field_validator(
        "raw_request",
        "selected_message_text",
        "selected_message_permalink",
        "channel_name",
        "thread_summary",
        "work_item_id",
        "intent",
        "feedback",
        mode="before",
    )
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("prior_agent_summaries", mode="before")
    @classmethod
    def _clean_summaries(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_clean_text(item, max_chars=320) for item in value[:_MAX_PRIOR_RUNS] if item]


class SlackQueryPromptSelection(BaseModel):
    """Rendered reusable prompt and routing metadata for a Slack request."""

    schema_: Literal["keystone.slack.query_prompt.v1"] = Field(
        default=SLACK_QUERY_PROMPT_SCHEMA,
        alias="schema",
    )
    version: str = SLACK_QUERY_PROMPT_VERSION
    kind: SlackQueryPromptKind
    target_route: WorkItemRoute
    task_brief: str
    context_flags: dict[str, bool] = Field(default_factory=dict)
    cost_profile: str = "standard"
    requires_approved_context: bool = False
    safety_notes: list[str] = Field(default_factory=list)
    dynamic_content_sha256: str = ""
    route_mismatch: dict[str, str] = Field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        """Return trace-safe metadata without the full prompt body."""

        payload = self.model_dump(mode="json", by_alias=True, exclude={"task_brief"})
        payload["task_brief_chars"] = len(self.task_brief)
        return payload


def build_slack_query_prompt_input(
    *,
    raw_request: str,
    selected_message_text: str = "",
    selected_message_permalink: str = "",
    channel_name: str = "",
    thread_summary: str = "",
    prior_agent_summaries: list[str] | None = None,
    manual_plan: dict[str, Any] | None = None,
    work_item_id: str = "",
    intent: str = "",
    target_route: WorkItemRoute | str | None = None,
    feedback: str = "",
) -> SlackQueryPromptInput:
    """Construct a bounded input model for Slack prompt resolution."""

    route = _coerce_route(target_route)
    if route is None and isinstance(manual_plan, dict):
        route = _coerce_route(manual_plan.get("target_agent"))
    return SlackQueryPromptInput(
        raw_request=raw_request,
        selected_message_text=selected_message_text,
        selected_message_permalink=selected_message_permalink,
        channel_name=channel_name,
        thread_summary=thread_summary,
        prior_agent_summaries=prior_agent_summaries or [],
        manual_plan=manual_plan,
        work_item_id=work_item_id,
        intent=intent,
        target_route=route,
        feedback=feedback,
    )


def resolve_slack_query_prompt(
    prompt_input: SlackQueryPromptInput,
) -> SlackQueryPromptSelection | None:
    """Return a reusable Slack prompt selection when the request shape is known."""

    request_text = prompt_input.raw_request
    if not request_text.strip() and not prompt_input.intent.strip():
        return None
    if _looks_like_unsafe_side_effect(request_text):
        return None

    detected = _detect_prompt_kind(prompt_input)
    if detected is None:
        return None

    detected_route = _default_route_for_kind(detected)
    target_route = _safe_target_route(prompt_input.target_route) or detected_route
    route_mismatch = {}
    if target_route != detected_route:
        route_mismatch = {
            "detected_route": detected_route.value,
            "deterministic_route": target_route.value,
            "resolution": "deterministic_route_wins",
        }

    context_flags = _context_flags_for_kind(detected)
    requires_approved_context = detected in {
        SlackQueryPromptKind.OUTREACH_DRAFT,
        SlackQueryPromptKind.CONTINUE_OR_REVISE,
    }
    safety_notes = _safety_notes_for_kind(detected)
    cost_profile = _cost_profile_for_kind(detected)
    task_brief = _render_task_brief(prompt_input, kind=detected, target_route=target_route)
    dynamic_hash = _dynamic_hash(
        {
            "kind": detected.value,
            "route": target_route.value,
            "raw_request": prompt_input.raw_request,
            "selected_message_text": prompt_input.selected_message_text,
            "thread_summary": prompt_input.thread_summary,
            "prior_agent_summaries": prompt_input.prior_agent_summaries,
            "feedback": prompt_input.feedback,
        }
    )
    return SlackQueryPromptSelection(
        kind=detected,
        target_route=target_route,
        task_brief=task_brief,
        context_flags=context_flags,
        cost_profile=cost_profile,
        requires_approved_context=requires_approved_context,
        safety_notes=safety_notes,
        dynamic_content_sha256=dynamic_hash,
        route_mismatch=route_mismatch,
    )


def slack_query_prompt_external_context(
    selection: SlackQueryPromptSelection | None,
) -> dict[str, Any]:
    """Return external-context payload for WorkItem execution."""

    if selection is None:
        return {}
    return {
        "schema": SLACK_QUERY_PROMPT_SCHEMA,
        "source": "slack_reusable_query_prompt",
        "slack_query_prompt": selection.model_dump(mode="json", by_alias=True),
    }


def _detect_prompt_kind(prompt_input: SlackQueryPromptInput) -> SlackQueryPromptKind | None:
    text = " ".join(
        item
        for item in (
            prompt_input.intent,
            prompt_input.raw_request,
            _manual_value(prompt_input.manual_plan, "intent"),
            _manual_value(prompt_input.manual_plan, "task_objective"),
            _manual_value(prompt_input.manual_plan, "expected_artifact_type"),
        )
        if item
    ).lower()
    if not text.strip():
        return None
    if any(marker in text for marker in ("revise", "revision", "continue", "run again", "redo")):
        return SlackQueryPromptKind.CONTINUE_OR_REVISE
    if any(marker in text for marker in ("more_research", "more research", "deeper", "deep read")):
        return SlackQueryPromptKind.DEEPER_RESEARCH
    if any(marker in text for marker in ("find_contact", "find contact", "contact discovery")):
        return SlackQueryPromptKind.DEEPER_RESEARCH
    if any(
        marker in text
        for marker in (
            "opportunity_search",
            "opportunity discovery",
            "opportunity",
            "rfp",
            "grant",
            "partnership",
            "pilot",
        )
    ):
        return SlackQueryPromptKind.OPPORTUNITY_SEARCH
    if any(
        marker in text
        for marker in ("outreach_draft", "outreach draft", "draft email", "linkedin", "compose")
    ):
        return SlackQueryPromptKind.OUTREACH_DRAFT
    if any(
        marker in text
        for marker in (
            "thread_summary",
            "thread summary",
            "next action",
            "next step",
            "summarize thread",
            "slack_operations",
            "what should",
        )
    ):
        return SlackQueryPromptKind.THREAD_SUMMARY_NEXT_ACTION
    if any(
        marker in text
        for marker in (
            "company_research",
            "research_brief",
            "source_research",
            "source_summary",
            "source-read",
            "source read",
            "research",
            "summarize",
            "compare",
            "source-backed",
            "source backed",
        )
    ):
        return SlackQueryPromptKind.RESEARCH_SUMMARY
    if prompt_input.target_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return SlackQueryPromptKind.RESEARCH_SUMMARY
    return None


def _render_task_brief(
    prompt_input: SlackQueryPromptInput,
    *,
    kind: SlackQueryPromptKind,
    target_route: WorkItemRoute,
) -> str:
    lines = [
        "Reusable Slack query task brief.",
        f"Prompt version: {SLACK_QUERY_PROMPT_VERSION}",
        f"Prompt kind: {kind.value}",
        f"Deterministic target route: {target_route.value}",
        "This brief is dynamic run input only; it does not grant tools, live access, or approval.",
        "",
        "Agent task:",
        _agent_task_for_kind(kind),
        "",
        "Operator request:",
        _clean_text(prompt_input.raw_request, max_chars=_MAX_RAW_REQUEST_CHARS),
    ]
    if prompt_input.feedback:
        lines.extend(["", "Operator feedback:", _clean_text(prompt_input.feedback, max_chars=600)])
    if prompt_input.work_item_id:
        lines.extend(["", "WorkItem reference:", prompt_input.work_item_id])
    comparison_instruction = _comparison_instruction(prompt_input.raw_request)
    if comparison_instruction:
        lines.extend(["", "Comparison handling:", comparison_instruction])
    if prompt_input.channel_name:
        lines.extend(["", "Slack channel:", prompt_input.channel_name])
    if prompt_input.selected_message_permalink:
        lines.extend(["", "Selected Slack permalink:", prompt_input.selected_message_permalink])
    if prompt_input.selected_message_text:
        lines.extend(
            [
                "",
                "Selected Slack message excerpt:",
                _clean_text(
                    prompt_input.selected_message_text,
                    max_chars=_MAX_CONTEXT_SNIPPET_CHARS,
                ),
            ]
        )
    if prompt_input.thread_summary:
        lines.extend(
            [
                "",
                "Slack thread context summary:",
                _clean_text(prompt_input.thread_summary, max_chars=_MAX_CONTEXT_SNIPPET_CHARS),
            ]
        )
    if prompt_input.prior_agent_summaries:
        lines.append("")
        lines.append("Prior agent run summaries:")
        for index, summary in enumerate(
            prompt_input.prior_agent_summaries[:_MAX_PRIOR_RUNS],
            start=1,
        ):
            lines.append(f"{index}. {_clean_text(summary, max_chars=320)}")
    lines.extend(
        [
            "",
            "Reusable prompt constraints:",
            "- Treat the original operator request as authoritative.",
            (
                "- Use existing context packs, source sufficiency checks, approval gates, "
                "and route gates."
            ),
            (
                "- If required source, recipient, approval, or record identity is missing, "
                "state the blocker."
            ),
        ]
    )
    return "\n".join(line for line in lines if line is not None).strip()


def _agent_task_for_kind(kind: SlackQueryPromptKind) -> str:
    tasks = {
        SlackQueryPromptKind.RESEARCH_SUMMARY: (
            "Produce a source-backed research summary that distinguishes verified facts, "
            "source gaps, and recommended next steps."
        ),
        SlackQueryPromptKind.DEEPER_RESEARCH: (
            "Deepen the existing research question with selected sources, source triage, "
            "and explicit gaps before synthesis."
        ),
        SlackQueryPromptKind.OPPORTUNITY_SEARCH: (
            "Find and prioritize opportunities using source-backed evidence, duplicate checks, "
            "and clear fit rationale."
        ),
        SlackQueryPromptKind.THREAD_SUMMARY_NEXT_ACTION: (
            "Summarize the selected Slack context and recommend the next safe Keystone action."
        ),
        SlackQueryPromptKind.OUTREACH_DRAFT: (
            "Draft outbound copy only from approved context; do not send, publish, schedule, "
            "or create external drafts."
        ),
        SlackQueryPromptKind.CONTINUE_OR_REVISE: (
            "Continue or revise the existing WorkItem while preserving prior approvals, "
            "blockers, and source constraints."
        ),
    }
    return tasks[kind]


def _comparison_instruction(raw_request: str) -> str:
    text = str(raw_request or "")
    if not re.search(r"\b(?:compare|comparison|table|versus|vs\.?)\b", text, flags=re.I):
        return ""
    target_count = "the requested number of"
    count_match = re.search(
        r"\b(?:two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\b",
        text,
        flags=re.I,
    )
    if count_match:
        target_count = count_match.group(0).lower()
    lines = [
        (
            f"Resolve {target_count} named products or companies as separate comparison "
            "targets before synthesis."
        ),
        (
            "Use source organizations, news articles, policy reports, blogs, and listicles as "
            "evidence only; do not promote them into comparison targets unless the operator "
            "explicitly asked to compare those source organizations."
        ),
        (
            "For category requests, run broad target discovery first, then target-specific "
            "official/product/help/policy/safety-page retrieval for each selected target."
        ),
        (
            "For AI companion or chatbot product comparisons, keep targets anchored to products "
            "that are themselves AI companions or chatbots; do not substitute adjacent teen-safety "
            "vendors, media outlets, regulators, or research organizations."
        ),
        (
            "Do not collapse a category request into one generic company profile; if separate "
            "current public product sources cannot be found, return exact source gaps."
        ),
    ]
    return "\n".join(f"- {line}" for line in lines)


def _context_flags_for_kind(kind: SlackQueryPromptKind) -> dict[str, bool]:
    flags = {
        "needs_identity_resolution": False,
        "needs_duplicate_check": False,
        "needs_source_attribution": False,
        "needs_source_triage": False,
        "needs_unsupported_claim_review": False,
        "needs_workspace_artifact": False,
        "needs_lifecycle_tracking": False,
        "needs_handoff": False,
    }
    if kind in {
        SlackQueryPromptKind.RESEARCH_SUMMARY,
        SlackQueryPromptKind.DEEPER_RESEARCH,
        SlackQueryPromptKind.OPPORTUNITY_SEARCH,
    }:
        flags.update(
            needs_identity_resolution=True,
            needs_source_attribution=True,
            needs_source_triage=True,
            needs_unsupported_claim_review=True,
        )
    if kind in {
        SlackQueryPromptKind.OPPORTUNITY_SEARCH,
        SlackQueryPromptKind.OUTREACH_DRAFT,
        SlackQueryPromptKind.CONTINUE_OR_REVISE,
    }:
        flags.update(needs_duplicate_check=True, needs_lifecycle_tracking=True)
    if kind == SlackQueryPromptKind.OUTREACH_DRAFT:
        flags.update(needs_identity_resolution=True, needs_source_attribution=True)
    if kind == SlackQueryPromptKind.THREAD_SUMMARY_NEXT_ACTION:
        flags.update(needs_lifecycle_tracking=True, needs_handoff=True)
    return {key: value for key, value in flags.items() if value}


def _cost_profile_for_kind(kind: SlackQueryPromptKind) -> str:
    if kind in {SlackQueryPromptKind.DEEPER_RESEARCH, SlackQueryPromptKind.OPPORTUNITY_SEARCH}:
        return "slack_research_deep"
    return "standard"


def _safety_notes_for_kind(kind: SlackQueryPromptKind) -> list[str]:
    notes = ["Reusable Slack prompt is advisory input; deterministic gates remain authoritative."]
    if kind == SlackQueryPromptKind.OUTREACH_DRAFT:
        notes.append("Outbound copy remains draft-only and approval-gated.")
    if kind == SlackQueryPromptKind.CONTINUE_OR_REVISE:
        notes.append("Existing WorkItem approval and blocker state must be preserved.")
    return notes


def _default_route_for_kind(kind: SlackQueryPromptKind) -> WorkItemRoute:
    if kind == SlackQueryPromptKind.OPPORTUNITY_SEARCH:
        return WorkItemRoute.OPPORTUNITY_SCOUT
    if kind == SlackQueryPromptKind.OUTREACH_DRAFT:
        return WorkItemRoute.OUTREACH_COMPOSER
    if kind == SlackQueryPromptKind.THREAD_SUMMARY_NEXT_ACTION:
        return WorkItemRoute.CHIEF_OF_STAFF
    return WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def _safe_target_route(route: WorkItemRoute | None) -> WorkItemRoute | None:
    if route in {None, WorkItemRoute.ORCHESTRATOR, WorkItemRoute.CLARIFICATION}:
        return None
    return route


def _coerce_route(value: WorkItemRoute | str | None) -> WorkItemRoute | None:
    if isinstance(value, WorkItemRoute):
        return value
    if value is None:
        return None
    try:
        return WorkItemRoute(str(value))
    except ValueError:
        return None


def _manual_value(manual_plan: dict[str, Any] | None, key: str) -> str:
    if not isinstance(manual_plan, dict):
        return ""
    return _clean_text(manual_plan.get(key))


def _looks_like_unsafe_side_effect(value: str) -> bool:
    text = str(value or "")
    matches = list(_SEND_ACTION_RE.finditer(text))
    if not matches:
        return False
    negated_spans = [match.span() for match in _NEGATED_SIDE_EFFECT_WINDOW_RE.finditer(text)]
    unsafe_matches = [
        match
        for match in matches
        if not any(start <= match.start() < end for start, end in negated_spans)
    ]
    if not unsafe_matches:
        return False
    if all("draft" in match.group(0).lower() for match in unsafe_matches):
        if _DRAFT_SAFE_RE.search(text):
            return False
    return True


def _dynamic_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_text(value: Any, *, max_chars: int = 2000) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."
