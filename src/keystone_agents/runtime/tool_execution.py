"""Stage-level evidence contracts for SDK function-tool execution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from keystone_agents.receipts.mutations import (
    operation_is_mutation,
    receipt_reports_possible_write,
)

ExternalWriteState = Literal["performed", "not_performed", "unknown"]


class ToolExecutionMode(StrEnum):
    """Whether one model stage requires, permits, or forbids tool execution."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ToolEvidenceGroup:
    """One evidence need satisfied by any completed tool in the group."""

    name: str
    any_of_tool_names: tuple[str, ...]
    min_successes: int = 1

    def __post_init__(self) -> None:
        names = tuple(dict.fromkeys(str(name).strip() for name in self.any_of_tool_names if name))
        if not self.name.strip():
            raise ValueError("Tool evidence groups require a name.")
        if not names:
            raise ValueError("Tool evidence groups require at least one tool name.")
        if self.min_successes < 1:
            raise ValueError("Tool evidence group min_successes must be positive.")
        object.__setattr__(self, "any_of_tool_names", names)


@dataclass(frozen=True)
class ToolExecutionContract:
    """Evidence postcondition for one SDK model stage."""

    mode: ToolExecutionMode
    required_groups: tuple[ToolEvidenceGroup, ...] = ()
    stage: str = "model_stage"

    def __post_init__(self) -> None:
        if self.mode is ToolExecutionMode.REQUIRED and not self.required_groups:
            raise ValueError("Required tool execution needs at least one evidence group.")
        if self.mode is not ToolExecutionMode.REQUIRED and self.required_groups:
            raise ValueError("Only required tool execution may define evidence groups.")

    @classmethod
    def required(
        cls,
        *groups: ToolEvidenceGroup,
        stage: str = "model_stage",
    ) -> ToolExecutionContract:
        return cls(
            mode=ToolExecutionMode.REQUIRED,
            required_groups=tuple(groups),
            stage=stage,
        )


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """Trace-safe evaluation of actual calls and provider receipts."""

    mode: ToolExecutionMode
    stage: str
    satisfied: bool
    attempted_tool_names: tuple[str, ...] = ()
    completed_tool_names: tuple[str, ...] = ()
    receipt_tool_names: tuple[str, ...] = ()
    missing_groups: tuple[str, ...] = ()
    prohibited_tool_names: tuple[str, ...] = ()

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "keystone.tool_execution_postcondition.v1",
            "mode": self.mode.value,
            "stage": self.stage,
            "satisfied": self.satisfied,
            "attempted_tool_names": list(self.attempted_tool_names),
            "completed_tool_names": list(self.completed_tool_names),
            "receipt_tool_names": list(self.receipt_tool_names),
            "missing_groups": list(self.missing_groups),
            "prohibited_tool_names": list(self.prohibited_tool_names),
        }


@dataclass(frozen=True)
class SDKToolExecutionRecord:
    """One SDK tool invocation paired with its output by call id."""

    call_id: str
    tool_name: str
    call_index: int
    output_index: int | None = None
    output_observed: bool = False
    succeeded: bool = False
    status: str = "attempted"
    self_contained_hosted: bool = False


class ToolExecutionContractError(RuntimeError):
    """Raised when a stage would otherwise claim success without required evidence."""

    def __init__(self, outcome: ToolExecutionOutcome) -> None:
        self.outcome = outcome
        if outcome.prohibited_tool_names:
            detail = "prohibited tools ran: " + ", ".join(outcome.prohibited_tool_names)
        else:
            detail = "missing evidence groups: " + ", ".join(outcome.missing_groups)
        super().__init__(f"Tool execution postcondition failed for {outcome.stage}: {detail}")


def build_tool_execution_summary(
    *,
    mode: str,
    selected_tool_names: Sequence[str] = (),
    model_called_tool_names: Sequence[str] = (),
    model_tool_call_count: int | None = None,
    workflow_called_tool_names: Sequence[str] = (),
    workflow_tool_call_count: int | None = None,
    workflow_called_helper_names: Sequence[str] = (),
    workflow_helper_call_count: int | None = None,
    preacquired_context_tool_names: Sequence[str] = (),
    preacquired_context_count: int | None = None,
    preacquired_context_source: str = "",
    provider_receipt_count: int | None = None,
    distinct_persisted_receipt_count: int | None = None,
    receipt_observation_count: int | None = None,
    tool_output_count: int | None = None,
    provider_request_attempt_count: int | None = None,
    provider_request_success_count: int | None = None,
    context_receipt_count: int = 0,
    context_receipt_source: str = "",
    scope_source: str = "",
    postcondition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one trace shape for model-, workflow-, and helper-managed tools.

    Provider attempts, successful requests, and durable receipts are separate
    counters. A successful network response is not itself evidence that a
    receipt was retained.
    """

    def names(values: Sequence[str]) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values if str(value).strip())
        )

    selected = names(selected_tool_names)
    model_called = names(model_called_tool_names)
    workflow_called = names(workflow_called_tool_names)
    helpers = names(workflow_called_helper_names)
    preacquired = names(preacquired_context_tool_names)
    provider_attempts_available = provider_request_attempt_count is not None
    provider_successes_available = provider_request_success_count is not None
    resolved_distinct_receipt_count = (
        distinct_persisted_receipt_count
        if distinct_persisted_receipt_count is not None
        else provider_receipt_count
    )
    provider_receipts_available = resolved_distinct_receipt_count is not None
    summary: dict[str, Any] = {
        "schema": "keystone.tool_execution_summary.v1",
        "mode": str(mode or "tool_free").strip() or "tool_free",
        "selected_tool_count": len(selected),
        "selected_tool_names": selected,
        "model_tool_call_count": max(
            len(model_called),
            int(model_tool_call_count) if model_tool_call_count is not None else 0,
        ),
        "model_called_tool_names": model_called,
        "tool_output_count": max(0, int(tool_output_count or 0)),
        "workflow_tool_call_count": max(
            len(workflow_called),
            int(workflow_tool_call_count) if workflow_tool_call_count is not None else 0,
        ),
        "workflow_called_tool_names": workflow_called,
        "workflow_helper_call_count": max(
            len(helpers),
            int(workflow_helper_call_count)
            if workflow_helper_call_count is not None
            else 0,
        ),
        "workflow_called_helper_names": helpers,
        "preacquired_context_count": max(
            len(preacquired),
            int(preacquired_context_count)
            if preacquired_context_count is not None
            else 0,
        ),
        "preacquired_context_tool_names": preacquired,
        "preacquired_context_source": str(preacquired_context_source or "").strip(),
        "provider_request_attempt_count": max(
            0,
            int(provider_request_attempt_count or 0),
        ),
        "provider_request_attempt_count_available": provider_attempts_available,
        "provider_request_success_count": max(
            0,
            int(provider_request_success_count or 0),
        ),
        "provider_request_success_count_available": provider_successes_available,
        # Compatibility alias: this counts distinct persisted receipt payloads,
        # not SDK calls, tool outputs, public projections, or provider requests.
        "provider_receipt_count": max(
            0, int(resolved_distinct_receipt_count or 0)
        ),
        "provider_receipt_count_available": provider_receipts_available,
        "distinct_persisted_receipt_count": max(
            0, int(resolved_distinct_receipt_count or 0)
        ),
        "receipt_observation_count": max(
            0,
            int(
                receipt_observation_count
                if receipt_observation_count is not None
                else resolved_distinct_receipt_count or 0
            ),
        ),
        "context_receipt_count": max(0, int(context_receipt_count)),
        "context_receipt_source": str(context_receipt_source or "").strip(),
        "tool_origins": {
            "attached": selected,
            "model_called": model_called,
            "workflow_called": workflow_called,
            "preacquired_context": preacquired,
            "deterministic_helper": helpers,
        },
    }
    if scope_source:
        summary["scope_source"] = str(scope_source).strip()
    if postcondition is not None:
        summary["postcondition"] = dict(postcondition)
    return summary


def external_write_state_from_evidence(
    *,
    receipts: Sequence[Mapping[str, Any] | Any] = (),
    tool_names: Sequence[str] = (),
    tool_invocations: Sequence[Mapping[str, Any] | Any] = (),
    evidence_complete: bool = False,
) -> ExternalWriteState:
    """Classify business-system writes without treating missing evidence as false.

    A verified or successful mutation receipt proves a write. A mutation call
    without conclusive receipt evidence remains unknown. ``not_performed`` is
    returned only when the captured call boundary is complete and contains no
    mutation, or every observed mutation is explicitly dry-run/blocked.
    """

    normalized_receipts = [
        receipt
        for value in receipts
        if isinstance(
            receipt := (
                value
                if isinstance(value, Mapping)
                else value.model_dump(mode="python")
                if callable(getattr(value, "model_dump", None))
                else None
            ),
            Mapping,
        )
    ]
    mutation_receipts = [
        receipt for receipt in normalized_receipts if receipt_reports_possible_write(receipt)
    ]
    if any(_receipt_proves_write(receipt) for receipt in mutation_receipts):
        return "performed"

    observed_tool_names = [str(name or "").strip() for name in tool_names]
    observed_tool_names.extend(
        str(item.get("tool_name") or "").strip()
        for item in tool_invocations
        if isinstance(item, Mapping)
    )
    mutation_tool_observed = any(operation_is_mutation(name) for name in observed_tool_names)
    if mutation_tool_observed or mutation_receipts:
        if mutation_receipts and all(
            _receipt_proves_no_write(receipt) for receipt in mutation_receipts
        ):
            return "not_performed"
        return "unknown"
    return "not_performed" if evidence_complete else "unknown"


def _receipt_proves_write(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("dry_run") is True:
        return False
    if receipt.get("provider_write") is True:
        return True
    status = str(receipt.get("status") or "").strip().lower().replace("_", "-")
    verification = receipt.get("verification")
    verified = bool(
        verification is True
        or isinstance(verification, Mapping)
        and (
            verification.get("passed") is True
            or verification.get("verified") is True
            or any(
                value is True
                for key, value in verification.items()
                if any(
                    marker in str(key).lower()
                    for marker in (
                        "created",
                        "deleted",
                        "updated",
                        "write",
                        "read_back",
                    )
                )
            )
        )
    )
    return bool(
        verified
        or status in {"completed", "done", "ok", "success", "succeeded", "verified"}
    )


def _receipt_proves_no_write(receipt: Mapping[str, Any]) -> bool:
    status = str(receipt.get("status") or "").strip().lower().replace("_", "-")
    return bool(
        receipt.get("dry_run") is True
        or receipt.get("provider_write") is False
        or status
        in {
            "blocked",
            "denied",
            "dry-run",
            "not-executed",
            "preview",
            "rejected",
            "skipped",
        }
    )


def evaluate_tool_execution_contract(
    raw_result: Any,
    contract: ToolExecutionContract,
    *,
    tool_receipts: Sequence[Mapping[str, Any] | Any] = (),
    preacquired_receipts: Sequence[Mapping[str, Any] | Any] = (),
    prior_attempted_tool_names: Sequence[str] = (),
    prior_completed_tool_names: Sequence[str] = (),
) -> ToolExecutionOutcome:
    """Evaluate completed calls across bounded attempts, never mere attachment.

    A validation-driven correction is a continuation of the same model stage.
    Successful evidence from its first attempt therefore remains valid, while
    callers still have to prevent the model from repeating the completed tool.
    """

    current_attempted, current_completed = _sdk_tool_execution(raw_result)
    attempted = tuple(
        dict.fromkeys(
            [
                *(str(name).strip() for name in prior_attempted_tool_names),
                *current_attempted,
            ]
        )
    )
    completed = tuple(
        dict.fromkeys(
            [
                *(str(name).strip() for name in prior_completed_tool_names),
                *current_completed,
            ]
        )
    )
    receipt_names = _successful_receipt_tool_names(
        [*tool_receipts, *preacquired_receipts]
    )
    evidence_names = set(completed) | set(receipt_names)
    if contract.mode is ToolExecutionMode.REQUIRED:
        missing = tuple(
            group.name
            for group in contract.required_groups
            if len(evidence_names.intersection(group.any_of_tool_names)) < group.min_successes
        )
        return ToolExecutionOutcome(
            mode=contract.mode,
            stage=contract.stage,
            satisfied=not missing,
            attempted_tool_names=attempted,
            completed_tool_names=completed,
            receipt_tool_names=receipt_names,
            missing_groups=missing,
        )
    if contract.mode is ToolExecutionMode.FORBIDDEN:
        prohibited = tuple(dict.fromkeys([*attempted, *completed]))
        return ToolExecutionOutcome(
            mode=contract.mode,
            stage=contract.stage,
            satisfied=not prohibited,
            attempted_tool_names=attempted,
            completed_tool_names=completed,
            receipt_tool_names=receipt_names,
            prohibited_tool_names=prohibited,
        )
    return ToolExecutionOutcome(
        mode=contract.mode,
        stage=contract.stage,
        satisfied=True,
        attempted_tool_names=attempted,
        completed_tool_names=completed,
        receipt_tool_names=receipt_names,
    )


def tool_execution_correction_prompt(
    contract: ToolExecutionContract,
    outcome: ToolExecutionOutcome,
    *,
    invocations: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build one bounded model correction from trace-safe tool evidence."""

    missing = set(outcome.missing_groups)
    missing_tools = {
        group.name: list(group.any_of_tool_names)
        for group in contract.required_groups
        if group.name in missing
    }
    invocation_evidence = [
        {
            "tool_name": str(item.get("tool_name") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "error_type": str(item.get("error_type") or "").strip(),
        }
        for item in invocations[-16:]
        if str(item.get("tool_name") or "").strip()
    ]
    evidence = {
        "stage": outcome.stage,
        "missing_evidence_groups": list(outcome.missing_groups),
        "allowed_tools_for_missing_groups": missing_tools,
        "attempted_tools": list(outcome.attempted_tool_names),
        "completed_tools": list(outcome.completed_tool_names),
        "receipt_tools": list(outcome.receipt_tool_names),
        "invocations": invocation_evidence,
    }
    return (
        "\n\nBounded tool-execution correction:\n"
        "The previous model attempt did not produce the required verified tool evidence. "
        "Use the original request and the attached tools to correct only the missing "
        "evidence groups below. Do not repeat a completed read. Never repeat a mutation "
        "that already produced a receipt. If a provider call failed, make at most one "
        "safe corrected call through an allowed tool or its configured provider fallback. "
        "Then return the requested typed output grounded in the resulting evidence. "
        "Do not claim success if the correction still fails.\n"
        f"Tool correction evidence: {json.dumps(evidence, ensure_ascii=True, sort_keys=True)}"
    )


def _sdk_tool_execution(raw_result: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    records = sdk_tool_execution_records(raw_result)
    attempted = tuple(dict.fromkeys(record.tool_name for record in records))
    completed = tuple(
        dict.fromkeys(record.tool_name for record in records if record.succeeded)
    )
    return attempted, completed


def sdk_tool_execution_records(raw_result: Any) -> tuple[SDKToolExecutionRecord, ...]:
    """Pair SDK call and output items without double-counting one invocation.

    Function tools emit a separate output item. Hosted Responses tools such as
    ``file_search`` instead return their status and results on the call item
    itself, so a completed hosted call is also its own observed output.
    """

    items = list(getattr(raw_result, "new_items", []) or [])
    if isinstance(raw_result, Mapping):
        items = list(raw_result.get("new_items") or raw_result.get("items") or [])
    calls: list[dict[str, Any]] = []
    outputs: dict[str, tuple[int, Any]] = {}
    for index, item in enumerate(items, start=1):
        item_type = _run_item_type(item)
        call_id = _run_item_call_id(item)
        if _is_tool_output_item_type(item_type):
            if call_id:
                outputs[call_id] = (index, item)
            continue
        if not _is_tool_call_item_type(item_type):
            continue
        tool_name = _run_item_tool_name(item)
        if not tool_name:
            continue
        resolved_call_id = call_id or f"item-{index}"
        calls.append(
            {
                "call_id": resolved_call_id,
                "tool_name": tool_name,
                "call_index": index,
                "item": item,
            }
        )

    records: list[SDKToolExecutionRecord] = []
    for call in calls:
        output_entry = outputs.get(str(call["call_id"]))
        if output_entry is None:
            call_status = _run_item_status(call["item"])
            self_contained = _is_self_contained_hosted_tool_call(call["item"])
            completed = self_contained and call_status == "completed"
            failed = call_status in _FAILED_TOOL_STATUSES
            records.append(
                SDKToolExecutionRecord(
                    call_id=str(call["call_id"]),
                    tool_name=str(call["tool_name"]),
                    call_index=int(call["call_index"]),
                    output_observed=completed,
                    succeeded=completed,
                    status=(
                        "completed"
                        if completed
                        else "failed"
                        if failed
                        else (call_status or "attempted")
                    ),
                    self_contained_hosted=self_contained,
                )
            )
            continue
        output_index, output_item = output_entry
        succeeded = _tool_output_succeeded(output_item)
        output_status = _tool_output_status(output_item)
        records.append(
            SDKToolExecutionRecord(
                call_id=str(call["call_id"]),
                tool_name=str(call["tool_name"]),
                call_index=int(call["call_index"]),
                output_index=output_index,
                output_observed=True,
                succeeded=succeeded,
                status=output_status or ("completed" if succeeded else "failed"),
            )
        )
    return tuple(records)


def sdk_tool_output_payloads(
    raw_result: Any,
    *,
    tool_name: str = "",
    successful_only: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Return JSON-object outputs paired to one SDK function-tool name.

    This is intentionally read-only and argument-free. It lets deterministic
    validators bind a model decision to the exact objects the model actually
    received without replaying the provider query or inspecting raw MIME/body
    content.
    """

    items = list(getattr(raw_result, "new_items", []) or [])
    if isinstance(raw_result, Mapping):
        items = list(raw_result.get("new_items") or raw_result.get("items") or [])
    call_names: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_type = _run_item_type(item)
        if not _is_tool_call_item_type(item_type):
            continue
        name = _run_item_tool_name(item)
        if not name:
            continue
        call_names[_run_item_call_id(item) or f"item-{index}"] = name

    successful_call_ids = (
        {
            record.call_id
            for record in sdk_tool_execution_records(raw_result)
            if record.succeeded
        }
        if successful_only
        else set()
    )
    payloads: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_type = _run_item_type(item)
        if not _is_tool_output_item_type(item_type):
            continue
        call_id = _run_item_call_id(item) or f"item-{index}"
        if successful_only and call_id not in successful_call_ids:
            continue
        name = call_names.get(call_id, "")
        if tool_name and name != tool_name:
            continue
        output = item.get("output") if isinstance(item, Mapping) else getattr(item, "output", None)
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                continue
        if isinstance(output, Mapping):
            payloads.append({"tool_name": name, "call_id": call_id, "output": dict(output)})
    return tuple(payloads)


def _run_item_type(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("type") or item.get("item_type") or "").strip().lower()
    return str(getattr(item, "type", "") or getattr(item, "item_type", "")).strip().lower()


def _is_tool_call_item_type(item_type: str) -> bool:
    return bool(
        item_type in {"tool_call_item", "function_call"}
        or (
            ("tool_call" in item_type or "function_call" in item_type)
            and "output" not in item_type
        )
    )


def _is_tool_output_item_type(item_type: str) -> bool:
    return bool("output" in item_type and ("tool" in item_type or "function" in item_type))


def _run_item_call_id(item: Any) -> str:
    if isinstance(item, Mapping):
        raw_item = item.get("raw_item")
        return str(
            item.get("call_id")
            or item.get("id")
            or (raw_item.get("call_id") if isinstance(raw_item, Mapping) else "")
            or (raw_item.get("id") if isinstance(raw_item, Mapping) else "")
            or ""
        )
    direct = getattr(item, "call_id", None)
    if direct:
        return str(direct)
    raw_item = getattr(item, "raw_item", None)
    if isinstance(raw_item, Mapping):
        return str(raw_item.get("call_id") or raw_item.get("id") or "")
    return str(
        getattr(raw_item, "call_id", None)
        or getattr(raw_item, "id", None)
        or ""
    )


def _run_item_tool_name(item: Any) -> str:
    if isinstance(item, Mapping):
        raw_item = item.get("raw_item")
        resolved = str(
            item.get("tool_name")
            or item.get("name")
            or (raw_item.get("tool_name") if isinstance(raw_item, Mapping) else "")
            or (raw_item.get("name") if isinstance(raw_item, Mapping) else "")
            or ""
        )[:160]
        if resolved:
            return resolved
        return _HOSTED_TOOL_NAMES_BY_RAW_TYPE.get(_run_item_raw_type(item), "")
    direct = getattr(item, "tool_name", None) or getattr(item, "name", None)
    if direct:
        return str(direct)[:160]
    raw_item = getattr(item, "raw_item", None)
    if isinstance(raw_item, Mapping):
        return str(raw_item.get("name") or raw_item.get("tool_name") or "")[:160]
    resolved = str(
        getattr(raw_item, "name", None)
        or getattr(raw_item, "tool_name", None)
        or ""
    )[:160]
    if resolved:
        return resolved
    return _HOSTED_TOOL_NAMES_BY_RAW_TYPE.get(_run_item_raw_type(item), "")


_HOSTED_TOOL_NAMES_BY_RAW_TYPE = {
    "file_search_call": "file_search",
}


def _run_item_raw_type(item: Any) -> str:
    raw_item = (
        item.get("raw_item")
        if isinstance(item, Mapping)
        else getattr(item, "raw_item", None)
    )
    if isinstance(raw_item, Mapping):
        return str(raw_item.get("type") or "").strip().lower()
    return str(getattr(raw_item, "type", "") or "").strip().lower()


def _is_self_contained_hosted_tool_call(item: Any) -> bool:
    return _run_item_raw_type(item) in _HOSTED_TOOL_NAMES_BY_RAW_TYPE


def _tool_output_succeeded(item: Any) -> bool:
    is_error = (
        item.get("is_error", False)
        if isinstance(item, Mapping)
        else getattr(item, "is_error", False)
    )
    if bool(is_error):
        return False
    output = item.get("output") if isinstance(item, Mapping) else getattr(item, "output", None)
    return tool_result_succeeded(output)


def tool_result_succeeded(output: Any) -> bool:
    """Classify one returned function-tool value as usable execution evidence."""

    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return bool(output.strip())
    if isinstance(output, Mapping):
        status = str(output.get("status") or "").strip().lower()
        if status in _FAILED_TOOL_STATUSES:
            return False
        if output.get("success") is False:
            return False
        verification = output.get("verification")
        if isinstance(verification, Mapping) and (
            verification.get("passed") is False
            or verification.get("verified") is False
        ):
            return False
    return output is not None


_FAILED_TOOL_STATUSES = frozenset(
    {
        "blocked",
        "canceled",
        "cancelled",
        "denied",
        "dry-run",
        "dry_run",
        "error",
        "empty",
        "failed",
        "failure",
        "no_results",
        "not_executed",
        "not_found",
        "partial",
        "pending",
        "preview",
        "rejected",
        "skipped",
        "timed_out",
        "timeout",
        "unavailable",
        "verification_failed",
    }
)


def _tool_output_status(item: Any) -> str:
    output = item.get("output") if isinstance(item, Mapping) else getattr(item, "output", None)
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            output = None
    if isinstance(output, Mapping):
        return str(output.get("status") or "").strip().lower()
    return _run_item_status(item)


def _run_item_status(item: Any) -> str:
    if isinstance(item, Mapping):
        raw_item = item.get("raw_item")
        return str(
            item.get("status")
            or item.get("state")
            or (raw_item.get("status") if isinstance(raw_item, Mapping) else "")
            or (raw_item.get("state") if isinstance(raw_item, Mapping) else "")
            or ""
        ).strip().lower()
    raw_item = getattr(item, "raw_item", None)
    return str(
        getattr(item, "status", None)
        or getattr(item, "state", None)
        or getattr(raw_item, "status", None)
        or getattr(raw_item, "state", None)
        or ""
    ).strip().lower()


def _successful_receipt_tool_names(
    receipts: Sequence[Mapping[str, Any] | Any],
) -> tuple[str, ...]:
    names: list[str] = []
    for value in receipts:
        receipt: Any = value
        if not isinstance(receipt, Mapping):
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                receipt = model_dump(mode="python")
        if not isinstance(receipt, Mapping):
            continue
        status = str(receipt.get("status") or "").strip().lower()
        verification = receipt.get("verification")
        verified = bool(
            isinstance(verification, Mapping)
            and (verification.get("passed") is True or verification.get("verified") is True)
        )
        if status not in {
            "completed",
            "done",
            "fixture",
            "ok",
            "read",
            "success",
            "succeeded",
            "verified",
        } and not verified:
            continue
        name = str(receipt.get("tool_name") or receipt.get("provider") or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


__all__ = [
    "ExternalWriteState",
    "SDKToolExecutionRecord",
    "ToolEvidenceGroup",
    "ToolExecutionContract",
    "ToolExecutionContractError",
    "ToolExecutionMode",
    "ToolExecutionOutcome",
    "build_tool_execution_summary",
    "evaluate_tool_execution_contract",
    "external_write_state_from_evidence",
    "sdk_tool_execution_records",
    "sdk_tool_output_payloads",
    "tool_result_succeeded",
]
