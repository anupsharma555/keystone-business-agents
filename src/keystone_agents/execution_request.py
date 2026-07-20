"""Normalize CLI, Slack, WorkItem, schedule, and direct-SDK asks."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any, cast

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.schemas.execution_request import (
    ExecutionContinuation,
    ExecutionEntrypoint,
    ExecutionPublicResult,
    ExecutionRequest,
    ExecutionResultStatus,
)

_SLACK_CONTINUATION_MARKER = "continue this prior slack thread"
_SLACK_FOLLOWUP_BOUNDARY_RE = re.compile(
    r"(?=\s+(?:Continue the same agent task\b|Previous request:|"
    r"Previous result title:|Previous result:|User follow-up:)|$)",
    re.IGNORECASE,
)


def latest_slack_operator_request(text: str) -> str:
    """Recover the latest operator ask while preserving explicit route advice."""

    raw = str(text or "").strip()
    original_mention = parse_agent_mention(
        raw,
        allow_bare_context_agents=True,
        allow_bare_agent_aliases=True,
    )
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
            followup_mention = parse_agent_mention(
                raw,
                allow_bare_context_agents=True,
                allow_bare_agent_aliases=True,
            )
            if original_mention.explicit and not followup_mention.explicit:
                route_prefixes = {
                    "orchestrator": "orchestrator",
                    "business_research_analyst": "business research analyst",
                    "opportunity_scout": "opportunity scout",
                    "outreach_composer": "outreach composer",
                    "gmail_triage": "gmail triage",
                    "chief_of_staff": "chief of staff",
                    "airtable_context_agent": "airtable context agent",
                    "google_workspace_context_agent": "google workspace context agent",
                    "zotero_context_agent": "zotero context agent",
                }
                prefix = route_prefixes.get(str(original_mention.route or ""), "")
                if prefix:
                    raw = f"{prefix} {raw}"

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
    return ExecutionRequest(
        entrypoint=cast(ExecutionEntrypoint, resolved_entrypoint),
        raw_request=raw,
        current_request=current_request,
        requested_agent=explicit_route,
        requested_agent_explicit=bool(requested_agent or mention.explicit),
        continuation=_slack_continuation(raw) if is_slack_followup else ExecutionContinuation(),
        source_context=source_context or {},
    )


def execution_request_planning_text(request: ExecutionRequest) -> str:
    """Combine bounded continuation context without weakening the latest ask."""

    current = request.current_request.strip()
    if request.entrypoint != "slack_followup":
        return current
    prior_request = request.continuation.prior_request.strip()[:4000]
    prior_result = request.continuation.prior_result_summary.strip()[:2400]
    # A typed provider continuation should re-read provider state. Previous bot
    # prose may describe a failed route and must not become task authority.
    if request.continuation.provider_affinity:
        prior_result = ""
    if not prior_request and not prior_result:
        return current
    parts = [prior_request] if prior_request else []
    if prior_result:
        parts.append(f"Prior result for context: {prior_result}")
    if current:
        parts.append(f"Authoritative follow-up: {current}")
    return "\n".join(parts)


def attach_execution_public_result(payload: dict[str, Any]) -> ExecutionPublicResult:
    """Attach one additive public-result contract while preserving legacy fields."""

    existing = payload.get("public_result")
    if isinstance(existing, Mapping):
        result = ExecutionPublicResult.model_validate(existing)
        _mirror_public_result(payload, result)
        return result

    text = _first_payload_text(
        payload,
        (
            "human_summary",
            "slack_display_text",
            "display_text",
            "summary",
            "message",
            "output.summary",
        ),
    )
    raw_status = str(payload.get("status") or payload.get("mode") or "").strip().lower()
    output = payload.get("output")
    output_mapping = output if isinstance(output, Mapping) else {}
    script_payload = payload.get("script_payload")
    script_mapping = script_payload if isinstance(script_payload, Mapping) else {}
    script_output = script_mapping.get("output")
    script_output_mapping = script_output if isinstance(script_output, Mapping) else {}
    audit_notes = [
        *(_string_list(output_mapping.get("audit_notes"))),
        *(_string_list(script_output_mapping.get("audit_notes"))),
    ]
    recovery_used = any(
        "deterministic chief of staff fallback was rendered instead" in note.lower()
        or "live sdk output failed validation" in note.lower()
        for note in audit_notes
    )
    missing_information = [
        *_string_list(payload.get("missing_information")),
        *_string_list(output_mapping.get("missing_information")),
        *_string_list(script_output_mapping.get("missing_information")),
    ]
    route_recommends_clarification = bool(
        str(output_mapping.get("recommended_route", {}).get("workflow_type") or "")
        == "clarification"
        if isinstance(output_mapping.get("recommended_route"), Mapping)
        else False
    )
    manual_plan = payload.get("manual_request_plan")
    manual_plan_mapping = (
        manual_plan if isinstance(manual_plan, Mapping) else {}
    )
    semantic_plan = str(manual_plan_mapping.get("source") or "") == "llm"
    plan_requires_clarification = bool(
        manual_plan_mapping.get("target_agent") == "clarification"
        or manual_plan_mapping.get("intent") == "clarification"
        or manual_plan_mapping.get("missing_required_information")
    )
    clarification = (
        raw_status == "needs_input"
        or bool(
            (route_recommends_clarification or plan_requires_clarification)
            and missing_information
        )
        or bool(
            not semantic_plan
            and re.search(
                r"\b(?:need clarification|not enough evidence to confirm|"
                r"missing context that .* could not safely infer)\b",
                text[:800],
                re.IGNORECASE | re.DOTALL,
            )
        )
    )
    failed = raw_status in {"failed", "timeout", "error"}
    blocked = raw_status in {"blocked", "needs_context", "needs_approval"}
    completion_confirmed = bool(
        payload.get("completion_confirmed")
        if "completion_confirmed" in payload
        else text and not failed and not blocked and not clarification
    )
    if failed or blocked or clarification:
        completion_confirmed = False

    receipts = _payload_receipts(payload)
    write_receipts = [receipt for receipt in receipts if _receipt_is_write(receipt)]
    side_effects = payload.get("side_effects")
    side_effect_mapping = side_effects if isinstance(side_effects, Mapping) else {}
    write_side_effect_reported = any(
        bool(value)
        and re.search(
            r"(?:create|update|delete|trash|send|post|modify|write|label)",
            str(key).lower(),
        )
        for key, value in side_effect_mapping.items()
    )
    provider_write_attempted = bool(write_receipts or write_side_effect_reported)
    provider_receipt_verified = (
        bool(write_receipts)
        and all(
            isinstance(receipt.get("verification"), Mapping)
            and receipt["verification"].get("passed") is True
            for receipt in write_receipts
        )
        if provider_write_attempted
        else None
    )
    if provider_write_attempted and provider_receipt_verified is not True:
        completion_confirmed = False

    status: ExecutionResultStatus
    if failed:
        status = "failed"
    elif clarification:
        status = "needs_input"
    elif blocked:
        status = "blocked"
    elif recovery_used and completion_confirmed:
        status = "recovered"
    elif completion_confirmed:
        status = "completed"
    else:
        status = "blocked"

    recovery_notice = (
        "Chief's structured result could not be validated. This answer was recovered "
        "through the safe fallback; no additional provider action was taken."
        if recovery_used
        else ""
    )
    output_failure = output_mapping.get("failure")
    output_failure_mapping = (
        output_failure if isinstance(output_failure, Mapping) else {}
    )
    failure_code = str(
        payload.get("block_kind")
        or output_mapping.get("error_type")
        or output_failure_mapping.get("code")
        or ""
    ).strip()
    failure_summary = (
        text if status in {"failed", "blocked", "needs_input"} else ""
    )
    title = str(payload.get("slack_display_title") or "").strip() or {
        "completed": "Business Agents Result Ready",
        "recovered": "Business Agents Result Recovered",
        "needs_input": "Business Agents Need Input",
        "blocked": "Business Agents Blocked",
        "failed": "Business Agents Run Failed",
    }[status]
    result = ExecutionPublicResult(
        status=status,
        title=title,
        omit_title=_completed_result_requires_title_omission(payload, status=status),
        text=text,
        completion_confirmed=completion_confirmed,
        provider_write_attempted=provider_write_attempted,
        provider_receipt_verified=provider_receipt_verified,
        recovery_used=recovery_used,
        recovery_notice=recovery_notice,
        failure_code=failure_code,
        failure_summary=failure_summary,
        run_id=str(payload.get("agent_run_id") or payload.get("run_id") or ""),
    )
    payload["public_result"] = result.model_dump(mode="json")
    _mirror_public_result(payload, result)
    return result


def _completed_result_requires_title_omission(
    payload: Mapping[str, Any],
    *,
    status: ExecutionResultStatus,
) -> bool:
    """Translate strict response counts into one renderer-neutral presentation rule."""

    if status != "completed":
        return False
    if _payload_has_canonical_internal_slack_artifact(payload):
        return True
    if _payload_is_provider_free_direct_context_answer(payload):
        return True
    plan = payload.get("manual_request_plan")
    if not isinstance(plan, Mapping):
        return False
    if (
        str(plan.get("provider_system") or "unspecified") != "unspecified"
        and str(plan.get("intent") or "")
        in {"business_system_write", "context_lookup"}
    ):
        return True
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, Mapping):
        return False
    constraints = ask_shape.get("output_constraints")
    if not isinstance(constraints, Mapping):
        return False
    required_sections = {
        str(section or "").strip().lower()
        for section in constraints.get("required_sections", [])
        if str(section or "").strip()
    } if constraints.get("require_section_headings") is True else set()
    if required_sections.intersection({"title", "heading", "headline"}):
        return False
    scope = str(constraints.get("scope") or "").strip().lower()
    if scope not in {"answer", "entire_response"}:
        return False
    return any(
        str(constraints.get(mode_field) or "").strip().lower() == "exact"
        and constraints.get(count_field) is not None
        for mode_field, count_field in (
            ("word_count_mode", "word_count"),
            ("sentence_count_mode", "sentence_count"),
            ("item_count_mode", "maximum_items"),
        )
    )


def _payload_is_provider_free_direct_context_answer(
    payload: Mapping[str, Any],
) -> bool:
    """Recognize a complete direct-answer plan without inspecting answer prose."""

    plan = payload.get("manual_request_plan")
    if not isinstance(plan, Mapping):
        return False
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, Mapping):
        return False
    workflow = plan.get("workflow")
    return bool(
        plan.get("intent") == "route_request"
        and isinstance(workflow, list)
        and not workflow
        and ask_shape.get("prior_context_dependency") == "selected_context"
        and plan.get("requires_live_search") is False
        and plan.get("requires_approved_context") is False
        and plan.get("side_effect_policy") == "draft_or_read_only"
    )


def _payload_has_canonical_internal_slack_artifact(
    payload: Mapping[str, Any],
) -> bool:
    """Recognize a reader-facing internal artifact without re-parsing prose."""

    candidates: list[Mapping[str, Any]] = [payload]
    for key in ("output", "script_payload", "work_item"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
            nested_output = value.get("output")
            if isinstance(nested_output, Mapping):
                candidates.append(nested_output)
            nested_work_item = value.get("work_item")
            if isinstance(nested_work_item, Mapping):
                candidates.append(nested_work_item)
    for candidate in candidates:
        artifacts = candidate.get("artifact_refs")
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            metadata = artifact.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            if (
                artifact.get("artifact_type") == "outreach_draft"
                and metadata.get("internal_slack_copy") is True
            ):
                return True
    return False


def _mirror_public_result(
    payload: dict[str, Any],
    result: ExecutionPublicResult,
) -> None:
    public_text = result.text
    if result.recovery_notice and result.recovery_notice not in public_text:
        public_text = (
            f"{public_text}\n\n{result.recovery_notice}"
            if public_text
            else result.recovery_notice
        )
    payload["slack_display_title"] = result.title
    payload["slack_display_text"] = public_text
    payload["display_text"] = public_text
    if public_text:
        payload["human_summary"] = public_text
        payload["summary"] = public_text
    payload["completion_confirmed"] = result.completion_confirmed
    payload.setdefault("user_facing_result_verified", bool(result.text))


def _first_payload_text(payload: Mapping[str, Any], paths: tuple[str, ...]) -> str:
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(part)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return ""


def _string_list(value: object) -> list[str]:
    values = value if isinstance(value, list | tuple | set) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _payload_receipts(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Collect bounded provider receipts from direct and child-run payloads."""

    receipts: list[Mapping[str, Any]] = []
    script_payload = payload.get("script_payload")
    script_mapping = script_payload if isinstance(script_payload, Mapping) else {}
    for source in (payload, script_mapping):
        direct = source.get("tool_receipt")
        if isinstance(direct, Mapping):
            receipts.append(direct)
        multiple = source.get("tool_receipts")
        if isinstance(multiple, list | tuple):
            receipts.extend(item for item in multiple if isinstance(item, Mapping))
    return receipts


def _receipt_is_write(receipt: Mapping[str, Any]) -> bool:
    operation = str(
        receipt.get("operation")
        or receipt.get("action")
        or receipt.get("operation_type")
        or ""
    ).lower()
    return bool(
        re.search(
            r"(?:^|_)(?:append|attach|create|delete|label|lifecycle|link|modify|"
            r"post|reconcile|remove|send|trash|update|upload|write)(?:_|$)",
            operation,
        )
    )


def _slack_continuation(raw_request: str) -> ExecutionContinuation:
    linked_ids = re.findall(
        r"\bLinked\s+WorkItem:\s*(wi_[A-Za-z0-9_-]+)\b",
        raw_request,
        flags=re.IGNORECASE,
    )
    prior_requests = _all_envelope_values(raw_request, "Previous request")
    provider_affinities = _all_envelope_values(raw_request, "Provider affinity")
    prior_titles = _all_envelope_values(raw_request, "Previous result title")
    prior_results = _all_envelope_values(raw_request, "Previous result")
    return ExecutionContinuation(
        work_item_id=linked_ids[-1] if linked_ids else "",
        provider_affinity=(
            provider_affinities[-1].lower() if provider_affinities else ""
        ),
        prior_request=prior_requests[-1] if prior_requests else "",
        prior_result_title=prior_titles[-1] if prior_titles else "",
        prior_result_summary=prior_results[-1] if prior_results else "",
    )


def _all_envelope_values(raw_request: str, label: str) -> list[str]:
    boundary = (
        r"(?=\s+(?:Current user request \(authoritative\):|Linked WorkItem:|"
        r"Provider affinity:|Previous request:|Previous result title:|"
        r"Previous result:|User follow-up:|Continue the same agent task\b)|$)"
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
    "attach_execution_public_result",
    "build_execution_request",
    "execution_request_planning_text",
    "latest_slack_operator_request",
]
