"""Normalize CLI, Slack, WorkItem, schedule, and direct-SDK asks."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any, cast

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.schemas.execution_request import (
    ContinuationObjectReference,
    ExecutionContinuation,
    ExecutionEntrypoint,
    ExecutionPublicResult,
    ExecutionRequest,
    ExecutionResultStatus,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.semantic_execution import (
    ExecutionIntentAuthority,
    StageOperationBoundary,
    StageOutputContract,
    reconcile_stage_output,
)
from keystone_agents.terminal_result_consistency import reconcile_failed_review

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
    """Normalize transport-only Slack mention decoration for turn equality.

    Slack root asks and thread replies can preserve the app mention as ``<@…>``,
    ``@KNI``, or a bare ``@ `` prefix while the child request contains only the
    operator's words.  Those forms are the same human turn.  Removing only that
    leading transport decoration prevents an identical prior ask from being
    replayed as context without changing meaningful mentions inside the request.
    """

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


def attach_execution_public_result(payload: dict[str, Any]) -> ExecutionPublicResult:
    """Attach one additive public-result contract while preserving legacy fields."""

    existing = payload.get("public_result")
    if isinstance(existing, Mapping):
        result = ExecutionPublicResult.model_validate(existing)
        result = reconcile_failed_review(payload, result)
        payload["public_result"] = result.model_dump(mode="json")
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
    plan_authority = ExecutionIntentAuthority.from_value(manual_plan)
    manual_plan_mapping = (
        plan_authority.plan.model_dump(mode="python")
        if plan_authority.plan is not None
        else {}
    )
    semantic_plan = plan_authority.canonical or plan_authority.invalid
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
    approval_pending = raw_status == "needs_approval"
    blocked = raw_status in {"blocked", "needs_context"}
    recovery_completion_confirmed = bool(
        payload.get("recovery_completion_confirmed")
    )
    completion_confirmed = bool(
        payload.get("completion_confirmed")
        if "completion_confirmed" in payload
        else text and not failed and not blocked and not clarification
    )
    if (
        failed
        or blocked
        or approval_pending
        or clarification
        or (recovery_used and not recovery_completion_confirmed)
    ):
        completion_confirmed = False

    receipts = _payload_receipts(payload)
    attach_verified_continuation_objects(payload, receipts)
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
    result_boundary = _public_result_operation_boundary(plan_authority)
    stage_output = reconcile_stage_output(
        payload,
        contract=StageOutputContract(
            stage="entrypoint_public_result",
            operation_boundary=result_boundary,
            public_output=True,
        ),
    )
    write_side_effect_reported = bool(
        write_side_effect_reported or stage_output.observed_effect_paths
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
    elif approval_pending:
        status = "needs_approval"
    elif blocked:
        status = "blocked"
    elif recovery_used and completion_confirmed:
        status = "recovered"
    elif recovery_used:
        status = "partial"
    elif completion_confirmed:
        status = "completed"
    else:
        status = "blocked"

    recovery_notice = (
        "Chief's structured result could not be validated. A safe deterministic "
        "fallback is shown for review, but the requested live result is not confirmed."
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
    failure_summary = text if status in {"failed", "blocked", "needs_input"} else ""
    if status == "partial":
        failure_summary = (
            "The live structured-output stage failed; the displayed fallback is "
            "reviewable but does not confirm live completion."
        )
    default_title = {
        "completed": "Business Agents Result Ready",
        "recovered": "Business Agents Result Recovered",
        "partial": "Business Agents Partial Result",
        "needs_approval": "Business Agents Awaiting Approval",
        "needs_input": "Business Agents Need Input",
        "blocked": "Business Agents Blocked",
        "failed": "Business Agents Run Failed",
    }[status]
    title = (
        default_title
        if status in {"partial", "needs_approval"}
        else str(payload.get("slack_display_title") or "").strip() or default_title
    )
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
    result = reconcile_failed_review(payload, result)
    payload["public_result"] = result.model_dump(mode="json")
    _mirror_public_result(payload, result)
    return result


def attach_verified_continuation_objects(
    payload: dict[str, Any],
    receipts: list[Mapping[str, Any]],
) -> tuple[ContinuationObjectReference, ...]:
    """Attach exact identities only from provider-verified internal receipts."""

    continuation_objects = _verified_object_references_from_receipts(receipts)
    if continuation_objects:
        payload["continuation_objects"] = [
            reference.model_dump(mode="json")
            for reference in continuation_objects
        ]
    return continuation_objects


def _public_result_operation_boundary(
    authority: ExecutionIntentAuthority,
) -> StageOperationBoundary:
    plan = authority.plan
    if plan is None:
        return StageOperationBoundary.READ_ONLY
    mutation_operations = {
        "create",
        "update",
        "delete",
        "trash",
        "send",
        "post",
        "write",
        "modify",
        "label",
    }
    if mutation_operations.intersection(
        authority.effective_provider_operations(plan.provider_system)
    ):
        return StageOperationBoundary.PROVIDER_WRITE
    if (
        plan.ask_shape.permission_state == "draft_only"
        or plan.ask_shape.output_form == "draft"
        or plan.expected_artifact_type == "outreach_draft"
    ):
        return StageOperationBoundary.DRAFT_ONLY
    return StageOperationBoundary.READ_ONLY


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


def _verified_object_references_from_receipts(
    receipts: list[Mapping[str, Any]],
) -> tuple[ContinuationObjectReference, ...]:
    """Project verified provider receipts into the shared continuation contract."""

    references: list[ContinuationObjectReference] = []
    for receipt in receipts:
        if not _receipt_verification_passed(receipt):
            continue
        reference = _verified_object_reference_from_receipt(receipt)
        if reference is None:
            continue
        if reference not in references:
            references.append(reference)
    return tuple(references[:8])


def _receipt_verification_passed(receipt: Mapping[str, Any]) -> bool:
    verification = receipt.get("verification")
    if isinstance(verification, Mapping):
        return verification.get("passed") is True
    if (
        receipt.get("provider_read") is True
        and str(receipt.get("status") or "").strip().lower() == "success"
        and any(
            str(receipt.get(key) or "").strip()
            for key in (
                "selected_item_key",
                "item_key",
                "event_id",
                "document_id",
                "record_id",
                "draft_id",
                "message_id",
                "thread_id",
            )
        )
    ):
        return True
    return bool(
        receipt.get("provider_verification") == "passed"
        and receipt.get("content_verified") is True
    )


def _verified_object_reference_from_receipt(
    receipt: Mapping[str, Any],
) -> ContinuationObjectReference | None:
    """Map verified provider receipt fields to one provider-neutral identity."""

    operation = str(
        receipt.get("operation")
        or receipt.get("action")
        or receipt.get("operation_type")
        or receipt.get("status")
        or ""
    ).strip().lower()
    verification = receipt.get("verification")
    verification_mapping = (
        verification if isinstance(verification, Mapping) else {}
    )
    deleted = bool(
        re.search(r"(?:^|_)(?:delete|deleted|trash|trashed|remove)(?:_|$)", operation)
        or receipt.get("trashed") is True
        or any(
            verification_mapping.get(key) is True
            for key in (
                "draft_absent_after_cleanup",
                "record_absent_after_cleanup",
                "note_absent_after_cleanup",
                "item_absent_after_cleanup",
                "document_trashed_after_cleanup",
                "record_absent_after",
                "item_absent_after",
            )
        )
    )

    event_id = str(receipt.get("event_id") or "").strip()
    if event_id:
        title = str(receipt.get("title") or receipt.get("summary") or "").strip()
        return ContinuationObjectReference(
            provider_system="google_calendar",
            object_type="calendar_event",
            object_id=event_id,
            display_name=title,
            effective_date=str(
                receipt.get("display_start_date")
                or receipt.get("start_date")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                {
                    **receipt,
                    "start_time": (
                        receipt.get("display_start_time")
                        or receipt.get("start_time")
                    ),
                    "end_time": (
                        receipt.get("display_end_time")
                        or receipt.get("end_time")
                    ),
                },
                ("calendar_id", "start_time", "end_time"),
            ),
        )

    document_id = str(receipt.get("document_id") or "").strip()
    if document_id:
        return ContinuationObjectReference(
            provider_system="google_drive",
            object_type="google_document",
            object_id=document_id,
            display_name=str(receipt.get("title") or "").strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("folder_path", "google_account"),
            ),
        )

    record_id = str(receipt.get("record_id") or "").strip()
    table = str(receipt.get("table") or "").strip()
    if record_id and table:
        return ContinuationObjectReference(
            provider_system="airtable",
            object_type="airtable_record",
            object_id=record_id,
            display_name=str(
                receipt.get("display_name")
                or receipt.get("title")
                or receipt.get("name")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("base_alias", "table"),
            ),
        )

    item_key = str(
        receipt.get("item_key") or receipt.get("selected_item_key") or ""
    ).strip()
    if item_key:
        is_note = bool(
            str(receipt.get("parent_item_key") or "").strip()
            or str(receipt.get("required_marker") or "").strip()
            or re.search(r"(?:^|_)(?:note|test_note)(?:_|$)", operation)
        )
        return ContinuationObjectReference(
            provider_system="zotero",
            object_type="zotero_note" if is_note else "zotero_item",
            object_id=item_key,
            display_name=str(
                receipt.get("selected_item_title")
                or receipt.get("title")
                or receipt.get("required_marker")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("library_id", "library_type", "parent_item_key"),
            ),
        )

    draft_id = str(receipt.get("draft_id") or "").strip()
    message_id = str(receipt.get("message_id") or "").strip()
    thread_id = str(receipt.get("thread_id") or "").strip()
    if draft_id or message_id or thread_id:
        sent = bool(
            receipt.get("sent") is True
            or re.search(r"(?:^|_)(?:send|sent)(?:_|$)", operation)
        )
        object_type = (
            "gmail_message"
            if sent and message_id
            else "gmail_thread"
            if sent and thread_id
            else "gmail_draft"
            if draft_id
            else "gmail_message"
            if message_id
            else "gmail_thread"
        )
        object_id = (
            message_id or thread_id
            if sent
            else draft_id or message_id or thread_id
        )
        provider_scope = _bounded_provider_scope(
            receipt,
            ("gmail_account",),
        )
        if thread_id and object_type != "gmail_thread":
            provider_scope["thread_id"] = thread_id
        if message_id and object_type == "gmail_draft":
            provider_scope["message_id"] = message_id
        if draft_id and sent:
            provider_scope["draft_id"] = draft_id
        return ContinuationObjectReference(
            provider_system="gmail",
            object_type=object_type,
            object_id=object_id,
            display_name=str(
                receipt.get("subject")
                or receipt.get("title")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=provider_scope,
        )
    return None


def _bounded_provider_scope(
    receipt: Mapping[str, Any],
    keys: tuple[str, ...],
) -> dict[str, str]:
    return {
        key: str(receipt.get(key) or "").strip()
        for key in keys
        if str(receipt.get(key) or "").strip()
    }


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
    """Return non-authoritative object hints from a continuation envelope.

    Raw Slack text cannot self-assert provider verification. Exact verified
    identities are rehydrated separately from locally persisted provider
    receipts. This parser keeps only bounded display/date hints for compatibility.
    """

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
    "attach_execution_public_result",
    "build_execution_request",
    "continuation_owner_advice",
    "execution_request_planning_text",
    "latest_slack_operator_request",
    "normalize_slack_operator_turn_identity",
    "slack_work_item_control_requested",
    "verified_continuation_objects",
]
