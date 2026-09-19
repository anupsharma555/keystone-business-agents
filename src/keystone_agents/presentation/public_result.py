"""Canonical boundary for assembling one entrypoint-neutral public result."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from keystone_agents.authority.semantic import (
    ExecutionIntentAuthority,
    StageOperationBoundary,
    StageOutputContract,
    reconcile_stage_output,
)
from keystone_agents.capabilities.profile import child_result_explicitly_unverified
from keystone_agents.instruction_following import (
    constraint_admission_has_unresolved,
    exact_output_validation_required,
    resolved_output_constraint_admission,
    validate_output_constraints,
)
from keystone_agents.presentation.consistency import reconcile_failed_review
from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.runtime.continuation import (
    attach_verified_continuation_objects,
)
from keystone_agents.schemas.execution_request import (
    ExecutionPublicResult,
    ExecutionResultStatus,
)

_NON_SUCCESS_STATUS_ALIASES: dict[str, ExecutionResultStatus] = {
    "blocked": "blocked",
    "canceled": "canceled",
    "cancelled": "canceled",
    "clarification_required": "needs_input",
    "error": "failed",
    "failed": "failed",
    "failure": "failed",
    "needs_approval": "needs_approval",
    "needs_context": "blocked",
    "needs_input": "needs_input",
    "partial": "partial",
    "timed_out": "failed",
    "timeout": "failed",
}
_VALIDATOR_NON_SUCCESS_ALIASES: dict[str, ExecutionResultStatus] = {
    "blocked": "blocked",
    "error": "failed",
    "failed": "failed",
    "rejected": "blocked",
    "repair_required": "blocked",
}
_PUBLIC_SUCCESS_STATUSES = {"verified", "completed", "recovered"}
_PUBLIC_NON_SUCCESS_STATUSES = {
    "partial",
    "needs_approval",
    "needs_input",
    "blocked",
    "failed",
    "canceled",
}


def ensure_work_item_user_facing_summary(result: Any) -> tuple[Any, bool]:
    """Reject bare workflow-status text without replacing a substantive answer."""
    summary = " ".join(str(getattr(result, "human_summary", "") or "").split())
    metadata_only = not summary or bool(
        re.fullmatch(
            r"(?:business agents\s+)?(?:work\s*item\s+)?"
            r"(?:command\s+)?(?:completed|complete|ready|done)[.!]?",
            summary,
            flags=re.I,
        )
    )
    if not metadata_only:
        return result, True
    blocker_messages = [
        " ".join(str(getattr(blocker, "message", "") or "").split())
        for blocker in list(getattr(result, "blockers", []) or [])
        if str(getattr(blocker, "message", "") or "").strip()
    ]
    replacement = (
        "I could not complete the requested work. "
        f"{blocker_messages[0]} Completion is not confirmed."
        if blocker_messages
        else "I could not verify a user-facing result for this run. Completion is "
        "not confirmed; review the agent trace before relying on it."
    )
    return result.model_copy(update={"human_summary": replacement}), False


def build_work_item_result_payload(
    result: Any,
    *,
    user_facing_result_verified: bool,
    graph_metadata: Mapping[str, Any] | None = None,
    execution_metadata: Mapping[str, Any] | None = None,
    public_telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare the shared JSON envelope from an already guarded saved result.

    Callers may add their own bounded metadata before attach_execution_public_result.
    This helper neither synthesizes an answer nor emits or delivers the response.
    """
    payload = result.model_dump(mode="json")
    status = str(getattr(result.status, "value", result.status))
    confirmed = bool(user_facing_result_verified and status == "done" and not result.blockers)
    payload.update(
        user_facing_result_verified=user_facing_result_verified,
        completion_confirmed=confirmed,
        slack_display_title=(
            "Business Agents Result Ready"
            if confirmed
            else "Business Agents Awaiting Approval"
            if status == "needs_approval"
            else "Business Agents Completion Not Confirmed"
        ),
        slack_display_text=result.human_summary,
        display_text=result.human_summary,
        summary=result.human_summary,
    )
    if any(
        getattr(artifact, "artifact_type", "") == "outreach_draft"
        and artifact.metadata.get("canonical_draft_copy") is True
        for artifact in getattr(result, "artifact_refs", [])
    ):
        # Preserve measured final-render evidence instead of losing verification
        # when the canonical draft skips another model synthesis call.
        admission = resolved_output_constraint_admission(
            getattr(result, "manual_request_plan", None),
            original_request=str(getattr(result.work_item, "request_text", "") or ""),
        )
        validation = validate_output_constraints(
            result.human_summary,
            admission.constraints,
        )
        payload["instruction_following"] = {"validation": validation.model_dump(mode="json")}
        if admission.warning_codes:
            payload["instruction_following"]["constraint_admission_warnings"] = list(
                admission.warning_codes
            )
        if validation.applicable and not validation.passed:
            payload["completion_confirmed"] = False
            payload["user_facing_result_verified"] = False
    if graph_metadata is not None:
        payload["_langgraph"] = dict(graph_metadata)
    if execution_metadata is not None:
        payload["_execution"] = dict(execution_metadata)
    if public_telemetry:
        payload.update(public_telemetry)
    return payload


@dataclass(frozen=True)
class _TerminalNonSuccessSignal:
    status: ExecutionResultStatus
    failure_code: str
    source: str


def attach_execution_public_result(
    payload: dict[str, Any],
) -> ExecutionPublicResult:
    """Attach one additive public-result contract while preserving legacy fields."""

    existing = payload.get("public_result")
    if isinstance(existing, Mapping):
        result = ExecutionPublicResult.model_validate(existing)
        result = _reconcile_terminal_non_success(payload, result)
        result = reconcile_failed_review(payload, result)
        result = _canonicalize_noncompleted_result_title(result)
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
    terminal_signal = _terminal_non_success_signal(payload)
    raw_status = (
        terminal_signal.status
        if terminal_signal is not None
        else str(payload.get("status") or payload.get("mode") or "").strip().lower()
    )
    if terminal_signal is not None:
        text = _terminal_signal_text(payload, terminal_signal)
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
        plan_authority.plan.model_dump(mode="python") if plan_authority.plan is not None else {}
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
            (route_recommends_clarification or plan_requires_clarification) and missing_information
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
    canceled = raw_status in {"canceled", "cancelled"}
    partial = raw_status == "partial"
    approval_pending = raw_status == "needs_approval"
    blocked = raw_status in {"blocked", "needs_context"}
    recovery_completion_confirmed = bool(payload.get("recovery_completion_confirmed"))
    completion_confirmed = bool(
        payload.get("completion_confirmed")
        if "completion_confirmed" in payload
        else text and not failed and not blocked and not clarification
    )
    if (
        failed
        or canceled
        or partial
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
    provider_write_required = result_boundary == StageOperationBoundary.PROVIDER_WRITE
    provider_receipt_verified = (
        bool(write_receipts)
        and all(
            isinstance(receipt.get("verification"), Mapping)
            and receipt["verification"].get("passed") is True
            for receipt in write_receipts
        )
        if provider_write_attempted
        else False
        if provider_write_required
        else None
    )
    if provider_write_required and provider_receipt_verified is not True:
        completion_confirmed = False
    elif provider_write_attempted and provider_receipt_verified is not True:
        completion_confirmed = False
    elif (
        recovery_used
        and provider_write_attempted
        and provider_receipt_verified is True
        and not (failed or blocked or approval_pending or clarification)
    ):
        # A malformed narrative cannot undo an already verified provider mutation.
        # Recover the public result from the durable receipt while keeping the model
        # failure visible in the recovery notice.
        recovery_completion_confirmed = True
        completion_confirmed = True

    status: ExecutionResultStatus
    if failed:
        status = "failed"
    elif canceled:
        status = "canceled"
    elif clarification:
        status = "needs_input"
    elif approval_pending:
        status = "needs_approval"
    elif blocked:
        status = "blocked"
    elif partial:
        status = "partial"
    elif recovery_used and completion_confirmed:
        status = "recovered"
    elif recovery_used:
        status = "partial"
    elif completion_confirmed:
        status = "completed"
    else:
        status = "blocked"

    recovery_notice = ""
    if recovery_used and completion_confirmed:
        recovery_notice = (
            "The structured agent result could not be validated, but the provider "
            "action was confirmed by a verified read-back receipt."
        )
    elif recovery_used:
        recovery_notice = (
            "The structured agent result could not be validated. A safe deterministic "
            "fallback is shown for review, but the requested live result is not confirmed."
        )
    output_failure = output_mapping.get("failure")
    output_failure_mapping = output_failure if isinstance(output_failure, Mapping) else {}
    failure_code = str(
        payload.get("block_kind")
        or output_mapping.get("error_type")
        or output_failure_mapping.get("code")
        or (terminal_signal.failure_code if terminal_signal is not None else "")
        or (
            "provider_write_receipt_required"
            if provider_write_required and provider_receipt_verified is not True
            else ""
        )
        or ""
    ).strip()
    failure_summary = text if status in {"failed", "canceled", "blocked", "needs_input"} else ""
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
        "canceled": "Business Agents Run Canceled",
    }[status]
    title = (
        default_title
        if status
        in {
            "recovered",
            "partial",
            "needs_approval",
            "needs_input",
            "blocked",
            "failed",
            "canceled",
        }
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
    result = _reconcile_terminal_non_success(payload, result)
    result = reconcile_failed_review(payload, result)
    result = _canonicalize_noncompleted_result_title(result)
    payload["public_result"] = result.model_dump(mode="json")
    _mirror_public_result(payload, result)
    return result


def public_result_storage_status(
    result: ExecutionPublicResult | Mapping[str, Any],
) -> str:
    """Map the canonical public result onto the existing agent-run status vocabulary."""

    public_result = (
        result
        if isinstance(result, ExecutionPublicResult)
        else ExecutionPublicResult.model_validate(result)
    )
    if public_result.status in _PUBLIC_SUCCESS_STATUSES:
        return "success"
    if public_result.status in {"failed", "canceled"}:
        return "error"
    return public_result.status


def _reconcile_terminal_non_success(
    payload: Mapping[str, Any],
    result: ExecutionPublicResult,
) -> ExecutionPublicResult:
    """Prevent child terminal evidence from being upgraded by a parent envelope."""

    signal = _terminal_non_success_signal(payload)
    if signal is None or result.status in _PUBLIC_NON_SUCCESS_STATUSES:
        return result
    text = _terminal_signal_text(payload, signal)
    failure_summary = "" if signal.status == "needs_approval" else text
    return result.model_copy(
        update={
            "status": signal.status,
            "title": _terminal_status_title(signal.status),
            "omit_title": False,
            "text": text,
            "completion_confirmed": False,
            "failure_code": result.failure_code or signal.failure_code,
            "failure_summary": result.failure_summary or failure_summary,
        }
    )


def _terminal_non_success_signal(
    payload: Mapping[str, Any],
) -> _TerminalNonSuccessSignal | None:
    containers = _terminal_evidence_containers(payload)
    for source, container in containers:
        raw_status = str(container.get("status") or "").strip().lower()
        status = _NON_SUCCESS_STATUS_ALIASES.get(raw_status)
        if status is not None:
            return _TerminalNonSuccessSignal(
                status=status,
                failure_code=str(
                    container.get("block_kind")
                    or container.get("reason_code")
                    or f"{source}_{raw_status}"
                ).strip(),
                source=source,
            )
        public_result = container.get("public_result")
        if isinstance(public_result, Mapping):
            raw_status = str(public_result.get("status") or "").strip().lower()
            status = _NON_SUCCESS_STATUS_ALIASES.get(raw_status)
            if status is not None:
                return _TerminalNonSuccessSignal(
                    status=status,
                    failure_code=str(
                        public_result.get("failure_code") or f"{source}_public_result_{raw_status}"
                    ).strip(),
                    source=f"{source}.public_result",
                )

    for source, container in containers:
        validation = _payload_instruction_following_validation(container)
        if validation.get("applicable") is True and validation.get("passed") is False:
            return _TerminalNonSuccessSignal(
                status="blocked",
                failure_code="instruction_following_constraint_failed",
                source=f"{source}.instruction_following.validation",
            )

    for source, container in containers:
        if child_result_explicitly_unverified(container):
            if source == "parent" and _parent_unverified_is_advisory(payload):
                continue
            return _TerminalNonSuccessSignal(
                status="blocked",
                failure_code=f"{source}_user_facing_result_unverified",
                source=f"{source}.user_facing_result_verified",
            )

    for source, container in containers:
        for key in ("execution_telemetry", "_execution_telemetry"):
            telemetry = container.get(key)
            if not isinstance(telemetry, Mapping):
                continue
            raw_status = str(telemetry.get("status") or "").strip().lower()
            status = _NON_SUCCESS_STATUS_ALIASES.get(raw_status)
            if status is not None:
                return _TerminalNonSuccessSignal(
                    status=status,
                    failure_code=f"{source}_{key}_{raw_status}",
                    source=f"{source}.{key}",
                )

    for source, container in containers:
        for owner_source, ownership in _decision_ownership_sources(container):
            outcome = ownership.get("validator_outcome")
            if not isinstance(outcome, Mapping):
                continue
            raw_status = str(outcome.get("status") or "").strip().lower()
            status = _VALIDATOR_NON_SUCCESS_ALIASES.get(raw_status)
            if status is not None:
                return _TerminalNonSuccessSignal(
                    status=status,
                    failure_code=str(
                        outcome.get("reason_code")
                        or f"{source}_{owner_source}_validator_{raw_status}"
                    ).strip(),
                    source=f"{source}.{owner_source}.validator_outcome",
                )
    return None


def _terminal_evidence_containers(
    payload: Mapping[str, Any],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    containers: list[tuple[str, Mapping[str, Any]]] = []
    script_payload = payload.get("script_payload")
    if isinstance(script_payload, Mapping):
        containers.append(("child", script_payload))
    containers.append(("parent", payload))
    for source, container in tuple(containers):
        sdk_failure = container.get("sdk_failure")
        if isinstance(sdk_failure, Mapping):
            containers.append((f"{source}.sdk_failure", sdk_failure))
    return tuple(containers)


def _decision_ownership_sources(
    container: Mapping[str, Any],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    sources: list[tuple[str, Mapping[str, Any]]] = []
    ownership = container.get("decision_ownership")
    if isinstance(ownership, Mapping):
        sources.append(("decision_ownership", ownership))
    request_cache = container.get("request_cache")
    if isinstance(request_cache, Mapping):
        ownership = request_cache.get("decision_ownership")
        if isinstance(ownership, Mapping):
            sources.append(("request_cache.decision_ownership", ownership))
    return tuple(sources)


def _terminal_signal_text(
    payload: Mapping[str, Any],
    signal: _TerminalNonSuccessSignal,
) -> str:
    if signal.source.endswith(".user_facing_result_verified"):
        return "The child result was explicitly marked unverified. Completion is not confirmed."
    if signal.source.startswith("child") and not (
        ".execution_telemetry" in signal.source or ".validator_outcome" in signal.source
    ):
        child = payload.get("script_payload")
        if isinstance(child, Mapping):
            text = _first_payload_text(
                child,
                ("human_summary", "output.failure.summary", "output.summary"),
            )
            if text:
                return text
    if signal.source == "parent":
        text = _first_payload_text(
            payload,
            ("human_summary", "output.failure.summary", "output.summary"),
        )
        if text:
            return text
    return {
        "partial": "Business Agents produced only a partial result; completion is not confirmed.",
        "needs_approval": "Business Agents prepared a result that still requires approval.",
        "needs_input": "Business Agents need additional input before continuing.",
        "blocked": "Business Agents could not validate a safe completed result.",
        "failed": "Business Agents did not complete this request.",
        "canceled": "Business Agents canceled this run before completion.",
    }[signal.status]


def _terminal_status_title(status: ExecutionResultStatus) -> str:
    return {
        "partial": "Business Agents Partial Result",
        "needs_approval": "Business Agents Awaiting Approval",
        "needs_input": "Business Agents Need Input",
        "blocked": "Business Agents Blocked",
        "failed": "Business Agents Run Failed",
        "canceled": "Business Agents Run Canceled",
        "verified": "Business Agents Result Ready",
        "completed": "Business Agents Result Ready",
        "recovered": "Business Agents Result Recovered",
    }[status]


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
    plan_mapping = plan if isinstance(plan, Mapping) else None
    raw_request = _payload_original_request(payload)
    condition_outcome = _payload_condition_outcome(payload)
    raw_or_planned_exact_output = exact_output_validation_required(
        plan_mapping,
        original_request=raw_request,
        condition_outcome=condition_outcome,
    )
    if plan_mapping is None:
        return raw_or_planned_exact_output
    if str(plan_mapping.get("provider_system") or "unspecified") != "unspecified" and str(
        plan_mapping.get("intent") or ""
    ) in {"business_system_write", "context_lookup"}:
        return True
    ask_shape = plan_mapping.get("ask_shape")
    if not isinstance(ask_shape, Mapping):
        return raw_or_planned_exact_output
    constraints = ask_shape.get("output_constraints")
    if not isinstance(constraints, Mapping):
        return raw_or_planned_exact_output
    required_sections = (
        {
            str(section or "").strip().lower()
            for section in constraints.get("required_sections", [])
            if str(section or "").strip()
        }
        if constraints.get("require_section_headings") is True
        else set()
    )
    if required_sections.intersection({"title", "heading", "headline"}):
        return False
    scope = str(constraints.get("scope") or "").strip().lower()
    planned_exact_output = scope in {"answer", "entire_response"} and any(
        str(constraints.get(mode_field) or "").strip().lower() == "exact"
        and constraints.get(count_field) is not None
        for mode_field, count_field in (
            ("word_count_mode", "word_count"),
            ("sentence_count_mode", "sentence_count"),
            ("item_count_mode", "maximum_items"),
        )
    )
    return raw_or_planned_exact_output or planned_exact_output


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
            f"{public_text}\n\n{result.recovery_notice}" if public_text else result.recovery_notice
        )
    if result.status in {
        "partial",
        "needs_approval",
        "needs_input",
        "blocked",
        "failed",
        "canceled",
    }:
        payload["status"] = result.status
    payload["slack_display_title"] = result.title
    payload["slack_display_text"] = public_text
    payload["display_text"] = public_text
    if public_text:
        payload["human_summary"] = public_text
        payload["summary"] = public_text
    payload["completion_confirmed"] = result.completion_confirmed
    validation = _payload_instruction_following_validation(payload)
    if any(
        child_result_explicitly_unverified(container)
        for _, container in _terminal_evidence_containers(payload)
    ):
        payload["user_facing_result_verified"] = False
    elif validation.get("applicable") is True and validation.get("passed") is False:
        payload["user_facing_result_verified"] = False
    elif _payload_has_unresolved_constraint_admission(payload):
        payload["user_facing_result_verified"] = _payload_has_independently_verified_child(payload)
    elif exact_output_validation_required(
        payload.get("manual_request_plan")
        if isinstance(payload.get("manual_request_plan"), Mapping)
        else None,
        original_request=_payload_original_request(payload),
        condition_outcome=_payload_condition_outcome(payload),
    ):
        validation = _payload_instruction_following_validation(payload)
        payload["user_facing_result_verified"] = bool(
            result.text
            and validation.get("applicable") is True
            and validation.get("passed") is True
        )
    else:
        payload.setdefault("user_facing_result_verified", bool(result.text))


def _payload_original_request(payload: Mapping[str, Any]) -> str:
    """Read the complete operator turn without substituting planner prose."""

    for candidate in (payload, payload.get("script_payload")):
        if not isinstance(candidate, Mapping):
            continue
        for key in ("input", "request_text", "raw_request", "original_request"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _payload_condition_outcome(payload: Mapping[str, Any]) -> bool | None:
    """Return an explicit yes/no specialist decision when one is present."""

    candidates: list[Mapping[str, Any]] = [payload]
    for key in ("output", "script_payload"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
            nested_output = value.get("output")
            if isinstance(nested_output, Mapping):
                candidates.append(nested_output)
    for candidate in candidates:
        value = candidate.get("needs_reply")
        if isinstance(value, bool):
            return value
    return None


def _payload_instruction_following_validation(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Find measured output validation without treating prose as proof."""

    for candidate in (payload, payload.get("script_payload")):
        if not isinstance(candidate, Mapping):
            continue
        instruction_following = candidate.get("instruction_following")
        if not isinstance(instruction_following, Mapping):
            continue
        validation = instruction_following.get("validation")
        if isinstance(validation, Mapping):
            return validation
    return {}


def _payload_has_unresolved_constraint_admission(
    payload: Mapping[str, Any],
) -> bool:
    """Keep incomplete count metadata from becoming a verification basis."""

    for candidate in (payload, payload.get("script_payload")):
        if not isinstance(candidate, Mapping):
            continue
        instruction_following = candidate.get("instruction_following")
        if not isinstance(instruction_following, Mapping):
            continue
        if constraint_admission_has_unresolved(
            instruction_following.get("constraint_admission_warnings")
        ):
            return True
    return False


def _payload_has_independently_verified_child(payload: Mapping[str, Any]) -> bool:
    """Recognize explicit successful child evidence despite parent-only advice."""

    child = payload.get("script_payload")
    if not isinstance(child, Mapping):
        return False
    public_result = child.get("public_result")
    return bool(
        child.get("user_facing_result_verified") is True
        and isinstance(public_result, Mapping)
        and str(public_result.get("status") or "") in _PUBLIC_SUCCESS_STATUSES
        and public_result.get("completion_confirmed") is True
    )


def _parent_unverified_is_advisory(payload: Mapping[str, Any]) -> bool:
    """Do not reinterpret a mirrored advisory false as a new terminal failure."""

    public_result = payload.get("public_result")
    validation = _payload_instruction_following_validation(payload)
    return bool(
        isinstance(public_result, Mapping)
        and str(public_result.get("status") or "") in _PUBLIC_SUCCESS_STATUSES
        and public_result.get("completion_confirmed") is True
        and _payload_has_unresolved_constraint_admission(payload)
        and not (validation.get("applicable") is True and validation.get("passed") is False)
    )


def _canonicalize_noncompleted_result_title(
    result: ExecutionPublicResult,
) -> ExecutionPublicResult:
    """Prevent a stale success heading from surviving a terminal downgrade."""

    replacement = {
        "partial": "Business Agents Partial Result",
        "needs_approval": "Business Agents Awaiting Approval",
        "needs_input": "Business Agents Need Input",
        "blocked": "Business Agents Blocked",
        "failed": "Business Agents Run Failed",
        "canceled": "Business Agents Run Canceled",
    }.get(result.status)
    current_title = result.title.strip()
    stale_success_titles = {
        "Business Agents Result Ready",
        "Business Agents Result Recovered",
    }
    if replacement and (not current_title or current_title in stale_success_titles):
        return result.model_copy(update={"title": replacement})
    return result


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
    return receipt_reports_possible_write(receipt)


__all__ = ["attach_execution_public_result", "public_result_storage_status"]
