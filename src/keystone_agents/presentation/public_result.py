"""Canonical boundary for assembling one entrypoint-neutral public result."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.authority.semantic import (
    ExecutionIntentAuthority,
    StageOperationBoundary,
    StageOutputContract,
    reconcile_stage_output,
)
from keystone_agents.contracts.completion import (
    deterministic_request_coverage,
    evaluate_deterministic_completion,
)
from keystone_agents.presentation.consistency import reconcile_failed_review
from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.schemas.execution_request import (
    ExecutionPublicResult,
    ExecutionResultStatus,
)


def attach_execution_public_result(
    payload: dict[str, Any],
) -> ExecutionPublicResult:
    """Attach one additive public-result contract while preserving legacy fields."""

    existing = payload.get("public_result")
    if isinstance(existing, Mapping):
        result = ExecutionPublicResult.model_validate(existing)
        original_status = result.status
        result = _reconcile_declared_terminal_status(payload, result)
        result = _reconcile_host_completion_coverage(payload, result)
        result = reconcile_failed_review(payload, result)
        if result.status != original_status:
            result = result.model_copy(
                update={
                    "title": _default_public_result_title(result.status),
                    "omit_title": _completed_result_requires_title_omission(
                        payload,
                        status=result.status,
                    ),
                }
            )
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
    raw_status = str(payload.get("status") or "").strip().lower()
    declared_status = _declared_terminal_status(payload)
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
    recovery_used = bool(
        declared_status == "recovered"
        or any(
            "deterministic chief of staff fallback was rendered instead" in note.lower()
            or "live sdk output failed validation" in note.lower()
            for note in audit_notes
        )
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
    failed = declared_status == "failed"
    blocked = declared_status == "blocked"
    partial = declared_status == "partial"
    canceled = declared_status == "canceled"
    completion_confirmed = bool(
        payload.get("completion_confirmed")
        if "completion_confirmed" in payload
        else text
        and declared_status in {None, "verified", "completed", "recovered"}
        and not clarification
    )
    if failed or blocked or partial or canceled or clarification:
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
    if clarification:
        status = "needs_input"
    elif declared_status in {"partial", "needs_input", "blocked", "failed", "canceled"}:
        status = declared_status
    elif declared_status is not None and completion_confirmed:
        status = declared_status
    elif declared_status is not None:
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
    provisional = ExecutionPublicResult(
        status=status,
        title="",
        text=text,
        completion_confirmed=completion_confirmed,
        provider_write_attempted=provider_write_attempted,
        provider_receipt_verified=provider_receipt_verified,
        recovery_used=recovery_used,
        recovery_notice=recovery_notice,
        failure_code=failure_code,
        failure_summary=(
            text
            if status in {"failed", "blocked", "needs_input", "partial", "canceled"}
            else ""
        ),
        run_id=str(payload.get("agent_run_id") or payload.get("run_id") or ""),
    )
    provisional = _reconcile_host_completion_coverage(payload, provisional)
    status = provisional.status
    completion_confirmed = provisional.completion_confirmed
    failure_code = provisional.failure_code
    failure_summary = provisional.failure_summary
    title = (
        str(payload.get("slack_display_title") or "").strip()
        or _default_public_result_title(status)
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


def _default_public_result_title(status: ExecutionResultStatus) -> str:
    return {
        "verified": "Business Agents Result Verified",
        "completed": "Business Agents Result Ready",
        "recovered": "Business Agents Result Recovered",
        "partial": "Business Agents Partially Completed",
        "needs_input": "Business Agents Need Input",
        "blocked": "Business Agents Blocked",
        "failed": "Business Agents Run Failed",
        "canceled": "Business Agents Run Canceled",
    }[status]


def _declared_terminal_status(
    payload: Mapping[str, Any],
) -> ExecutionResultStatus | None:
    """Normalize an explicit terminal state without treating execution mode as one."""

    aliases: dict[str, ExecutionResultStatus] = {
        "verified": "verified",
        "completed": "completed",
        "complete": "completed",
        "done": "completed",
        "success": "completed",
        "succeeded": "completed",
        "recovered": "recovered",
        "partial": "partial",
        "needs_input": "needs_input",
        "clarification_required": "needs_input",
        "blocked": "blocked",
        "needs_context": "blocked",
        "needs_approval": "blocked",
        "pending_approval": "blocked",
        "awaiting_approval": "blocked",
        "failed": "failed",
        "error": "failed",
        "timeout": "failed",
        "rejected": "failed",
        "canceled": "canceled",
        "cancelled": "canceled",
    }
    if "status" in payload:
        raw = str(payload.get("status") or "").strip().lower()
        if not raw:
            return None
        return aliases.get(raw, "blocked")
    mode = str(payload.get("mode") or "").strip().lower()
    return aliases.get(mode)


def _reconcile_declared_terminal_status(
    payload: Mapping[str, Any],
    result: ExecutionPublicResult,
) -> ExecutionPublicResult:
    """Prevent outer or existing non-success state from being promoted."""

    declared = _declared_terminal_status(payload)
    non_success_priority: dict[ExecutionResultStatus, int] = {
        "partial": 1,
        "needs_input": 2,
        "blocked": 2,
        "canceled": 3,
        "failed": 4,
    }
    existing_non_success = result.status in non_success_priority
    declared_non_success = declared in non_success_priority
    if existing_non_success and not declared_non_success:
        return result.model_copy(update={"completion_confirmed": False})
    if not declared_non_success:
        return result
    status = declared
    if existing_non_success and (
        non_success_priority[result.status] >= non_success_priority[declared]
    ):
        status = result.status
    return result.model_copy(
        update={
            "status": status,
            "completion_confirmed": False,
            "failure_summary": result.failure_summary or result.text,
        }
    )


def _reconcile_host_completion_coverage(
    payload: Mapping[str, Any],
    result: ExecutionPublicResult,
) -> ExecutionPublicResult:
    """Downgrade only a success-family result when host coverage is incomplete."""

    if result.status in {
        "partial",
        "needs_input",
        "blocked",
        "failed",
        "canceled",
    }:
        return result.model_copy(update={"completion_confirmed": False})

    coverage = deterministic_request_coverage(payload)
    if coverage is None:
        return result
    decision = evaluate_deterministic_completion([coverage])
    if decision.completion_allowed:
        return result
    status: ExecutionResultStatus = (
        "partial" if coverage.status == "partial" and bool(result.text) else "blocked"
    )
    return result.model_copy(
        update={
            "status": status,
            "completion_confirmed": False,
            "failure_code": result.failure_code or decision.reason_code,
            "failure_summary": result.failure_summary or result.text,
        }
    )


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
    return receipt_reports_possible_write(receipt)


__all__ = ["attach_execution_public_result"]
