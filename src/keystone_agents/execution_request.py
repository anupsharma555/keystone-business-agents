"""Normalize CLI, Slack, WorkItem, schedule, and direct-SDK asks."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import cast

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.presentation.public_result import attach_execution_public_result
from keystone_agents.runtime.continuation import (
    attach_verified_continuation_objects,
)
from keystone_agents.schemas.execution_request import (
    ContinuationObjectReference,
    ExecutionContinuation,
    ExecutionEntrypoint,
    ExecutionRequest,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

_SLACK_CONTINUATION_MARKER = "continue this prior slack thread"
_SLACK_FOLLOWUP_BOUNDARY_RE = re.compile(
    r"(?=\s+(?:Continue the same agent task\b|Prior task owner \(advisory\):|Previous request:|"
    r"Previous result title:|Previous result:|User follow-up:)|$)",
    re.IGNORECASE,
)


def latest_slack_operator_request(text: str) -> str:
    """Recover the latest operator ask while preserving explicit route advice."""

    raw = str(text or "").strip()
    if _SLACK_CONTINUATION_MARKER in raw.lower():
        followups = [
            match.group(1).strip()
            for match in re.finditer(
                r"User follow-up:\s*(.*?)" + _SLACK_FOLLOWUP_BOUNDARY_RE.pattern,
                raw,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if match.group(1).strip()
        ]
        if followups:
            raw = followups[-1]

    raw = html.unescape(raw).strip()
    raw = re.sub(r"^\s*>\s*", "", raw)
    raw = re.sub(r"^\s*@\s+(?=[A-Za-z])", "", raw)
    return _strip_slack_entrypoint_decoration(raw)


def build_execution_request(
    raw_request: str,
    *,
    requested_agent: str | None = None,
    slack_context_input: bool = False,
    entrypoint: ExecutionEntrypoint | None = None,
    source_context: dict[str, str] | None = None,
) -> ExecutionRequest:
    """Build the canonical request without using the entrypoint as route authority."""

    raw = str(raw_request or "").strip()
    is_slack_followup = _SLACK_CONTINUATION_MARKER in raw.lower()
    operator_input = (
        latest_slack_operator_request(raw)
        if slack_context_input or is_slack_followup
        else raw
    )
    mention = parse_agent_mention(
        operator_input,
        allow_bare_context_agents=True,
        allow_bare_agent_aliases=True,
    )
    explicit_route = str(requested_agent or mention.route or "").strip()
    current_request = (
        mention.input_text
        if mention.explicit and (requested_agent is None or is_slack_followup)
        else operator_input
    )
    resolved_entrypoint = entrypoint or (
        "slack_followup"
        if is_slack_followup
        else "slack_root"
        if slack_context_input
        else "cli"
    )
    continuation = (
        _slack_continuation(raw, current_request=current_request)
        if is_slack_followup
        else ExecutionContinuation()
    )
    return ExecutionRequest(
        entrypoint=cast(ExecutionEntrypoint, resolved_entrypoint),
        raw_request=raw,
        current_request=current_request,
        requested_agent=explicit_route,
        requested_agent_explicit=bool(requested_agent or mention.explicit),
        continuation=continuation,
        source_context=source_context or {},
    )


def execution_request_planning_text(request: ExecutionRequest) -> str:
    """Combine bounded continuation context without weakening the latest ask."""

    current = request.current_request.strip()
    if request.entrypoint != "slack_followup":
        return current
    prior_request = request.continuation.prior_request.strip()[:4000]
    prior_result = request.continuation.prior_result_summary.strip()[:2400]
    if _normalized_request_identity(prior_request) == _normalized_request_identity(current):
        prior_request = ""
    # A typed provider continuation should re-read provider state. Previous bot
    # prose may describe a failed route and must not become task authority.
    if request.continuation.provider_affinity:
        prior_result = ""
    verified_objects = request.continuation.verified_objects
    if not prior_request and not prior_result and not verified_objects:
        return current
    parts: list[str] = []
    if prior_request:
        parts.append(prior_request)
    if prior_result:
        parts.append(f"Prior result for context: {prior_result}")
    parts.extend(
        _verified_object_planning_line(reference)
        for reference in verified_objects
    )
    if current:
        parts.append(f"Authoritative follow-up: {current}")
    return "\n".join(parts)


def _normalized_request_identity(value: str) -> str:
    return normalize_slack_operator_turn_identity(value)


def normalize_slack_operator_turn_identity(value: object) -> str:
    """Normalize transport-only Slack mention decoration for turn equality."""

    clean = html.unescape(str(value or "").strip())
    clean = re.sub(
        r"^\s*(?:<@[^>]+>|@KNI\b|@(?=\s))\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    return " ".join(clean.casefold().split())


def continuation_owner_advice(
    request: ExecutionRequest,
    plan: ManualRequestPlan,
) -> str:
    """Retain one prior owner only for a semantic same-artifact continuation.

    The LLM plan establishes that prior context is needed and that the current
    turn remains a provider-free response transformation. This tie-breaker does
    not inspect trigger words, grant tool authority, or override a newly named
    agent, provider action, draft, plan, workflow, or durable task.
    """

    prior_agent = request.continuation.prior_agent.strip()
    if not prior_agent or request.requested_agent_explicit:
        return ""
    if plan.ask_shape.prior_context_dependency not in {"selected_context", "required"}:
        return ""
    if plan.ask_shape.output_form in {"draft", "plan"}:
        return ""
    if plan.provider_system != "unspecified" or plan.provider_operations:
        return ""
    if plan.workflow or plan.requires_durable_state:
        return ""
    if plan.side_effect_policy != "draft_or_read_only":
        return ""
    if plan.intent in {
        "blocked_send",
        "business_system_write",
        "clarification",
        "continue_work_item",
        "opportunity_to_outreach_loop",
        "outreach_draft",
    }:
        return ""
    if plan.expected_artifact_type == "outreach_draft":
        return ""
    return prior_agent


def slack_work_item_control_requested(current_request: str) -> bool:
    """Return whether the current human turn explicitly controls prior state.

    Slack adapters may wrap every thread reply as a continuation. That transport
    marker must not make a linked WorkItem authoritative for an ordinary natural-
    language ask. Only an explicit current-turn state-control instruction may
    preserve the linked ID for resume/retry/approval handling.
    """

    normalized = " ".join(str(current_request or "").strip().lower().split())
    return bool(
        re.match(
            r"^(?:please\s+)?(?:continue|resume|retry|run\s+again|approve|reject)\b",
            normalized,
        )
    )


def _slack_continuation(
    raw_request: str,
    *,
    current_request: str = "",
) -> ExecutionContinuation:
    linked_ids = re.findall(
        r"\bLinked\s+WorkItem:\s*(wi_[A-Za-z0-9_-]+)\b",
        raw_request,
        flags=re.IGNORECASE,
    )
    prior_requests = _all_envelope_values(raw_request, "Previous request")
    prior_agents = _all_envelope_values(raw_request, "Prior task owner (advisory)")
    provider_affinities = _all_envelope_values(raw_request, "Provider affinity")
    prior_titles = _all_envelope_values(raw_request, "Previous result title")
    prior_results = _all_envelope_values(raw_request, "Previous result")
    return ExecutionContinuation(
        work_item_id=(
            linked_ids[-1]
            if linked_ids and slack_work_item_control_requested(current_request)
            else ""
        ),
        prior_agent=(
            prior_agents[-1].lower()
            if prior_agents
            else _slack_envelope_prior_agent(raw_request)
        ),
        provider_affinity=(
            provider_affinities[-1].lower() if provider_affinities else ""
        ),
        prior_request=prior_requests[-1] if prior_requests else "",
        prior_result_title=prior_titles[-1] if prior_titles else "",
        prior_result_summary=prior_results[-1] if prior_results else "",
        verified_objects=verified_continuation_objects(raw_request),
    )


def verified_continuation_objects(
    raw_request: str,
) -> tuple[ContinuationObjectReference, ...]:
    """Return non-authoritative object hints from a continuation envelope."""

    references: list[ContinuationObjectReference] = []
    for raw_value in _all_envelope_values(raw_request, "Previous verified objects"):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        values = parsed if isinstance(parsed, list) else [parsed]
        for value in values:
            if not isinstance(value, Mapping):
                continue
            try:
                reference = ContinuationObjectReference.model_validate(value)
            except ValueError:
                continue
            reference = reference.model_copy(
                update={
                    "object_id": "",
                    "provider_scope": {},
                    "verification_status": "unverified",
                }
            )
            if reference not in references:
                references.append(reference)
    if references:
        return tuple(references[:8])

    prior_results = _all_envelope_values(raw_request, "Previous result")
    calendar_pattern = re.compile(
        r"\bGoogle Calendar event\s+"
        r"(?P<operation>created|updated|deleted)\s+and\s+verified\s*:\s*"
        r"[\"“](?P<title>.+?)[\"”]"
        r"(?:\s+on\s+(?P<date>20\d{2}-\d{2}-\d{2}))?",
        re.IGNORECASE,
    )
    for result in reversed(prior_results):
        match = calendar_pattern.search(result)
        if not match:
            continue
        operation = match.group("operation").lower()
        return (
            ContinuationObjectReference(
                provider_system="google_calendar",
                object_type="calendar_event",
                display_name=match.group("title").strip(),
                effective_date=str(match.group("date") or ""),
                lifecycle_state="deleted" if operation == "deleted" else "active",
                verification_status="unverified",
            ),
        )
    return ()


def _verified_object_planning_line(
    reference: ContinuationObjectReference,
) -> str:
    fields = [
        f"provider={reference.provider_system}",
        f"type={reference.object_type}",
        f"state={reference.lifecycle_state}",
        f"verification={reference.verification_status}",
    ]
    if reference.object_id:
        fields.append(f"id={reference.object_id}")
    if reference.display_name:
        fields.append(f"name={reference.display_name}")
    if reference.effective_date:
        fields.append(f"date={reference.effective_date}")
    fields.extend(
        f"scope.{key}={value}"
        for key, value in sorted(reference.provider_scope.items())
    )
    prefix = (
        "Prior verified object for context"
        if reference.verification_status == "verified"
        else "Unverified prior object hint"
    )
    return f"{prefix}: " + ", ".join(fields)


def _slack_envelope_prior_agent(raw_request: str) -> str:
    """Recover an adapter's prior owner without making it a current-turn mention."""

    marker_index = str(raw_request or "").lower().find(_SLACK_CONTINUATION_MARKER)
    if marker_index < 0:
        return ""
    envelope_prefix = str(raw_request or "")[:marker_index].strip()
    mention = parse_agent_mention(
        envelope_prefix,
        allow_bare_context_agents=True,
        allow_bare_agent_aliases=True,
    )
    return str(mention.route or "").strip() if mention.explicit else ""


def _all_envelope_values(raw_request: str, label: str) -> list[str]:
    boundary = (
        r"(?=\s+(?:Current user request \(authoritative\):|Linked WorkItem:|"
        r"Prior task owner \(advisory\):|Provider affinity:|Previous request:|"
        r"Previous result title:|"
        r"Previous result:|Previous verified objects:|User follow-up:|"
        r"Continue the same agent task\b)|$)"
    )
    return [
        _strip_slack_entrypoint_decoration(" ".join(match.group(1).split()))
        for match in re.finditer(
            rf"{re.escape(label)}:\s*(.*?){boundary}",
            raw_request,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match.group(1).strip()
    ]


def _strip_slack_entrypoint_decoration(value: str) -> str:
    """Remove connector attribution without changing the operator's ask."""

    return re.sub(
        r"\s*\*?Sent using\*?(?:\s+<@[^>]+>)?\s*$",
        "",
        str(value or "").strip(),
        flags=re.IGNORECASE,
    ).strip()


__all__ = [
    "attach_verified_continuation_objects",
    "attach_execution_public_result",
    "build_execution_request",
    "continuation_owner_advice",
    "execution_request_planning_text",
    "latest_slack_operator_request",
    "normalize_slack_operator_turn_identity",
    "slack_work_item_control_requested",
    "verified_continuation_objects",
]
