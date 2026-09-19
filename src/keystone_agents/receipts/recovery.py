"""Durable provider mutation receipts and idempotent downstream recovery."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.receipts.normalization import (
    normalize_provider_mutation_receipt,
    payload_digest,
)
from keystone_agents.schemas.provider_recovery import (
    ProviderRecoveryState,
)

T = TypeVar("T")

_CHECKPOINT_SCHEMA = "keystone.provider_recovery_checkpoint.v1"
_FAILED_MUTATION_STATUSES = {
    "blocked",
    "cancelled",
    "denied",
    "error",
    "failed",
    "rejected",
}


class ProviderRecoveryError(RuntimeError):
    """Base error for invalid or conflicting provider recovery state."""


class ProviderRecoveryConflictError(ProviderRecoveryError):
    """The same idempotency key resolved to incompatible provider identities."""


class ProviderPartialSuccessError(ProviderRecoveryError):
    """A provider mutation completed before a downstream stage failed."""

    def __init__(
        self,
        state: ProviderRecoveryState,
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.partial_success = state
        self.cause = cause
        object_id = next(
            (receipt.object_id for receipt in state.receipts if receipt.object_id),
            "",
        )
        identity = f" Existing provider object: {object_id}." if object_id else ""
        super().__init__(
            f"Provider mutation completed, but {state.failed_stage} failed.{identity} "
            f"{state.safe_next_action}".strip()
        )


class ProviderRecoveryStore:
    """Atomic local checkpoint for one caller-scoped idempotency key."""

    def __init__(self, path: str | Path, *, idempotency_key: str) -> None:
        self.path = Path(path)
        self.idempotency_key = str(idempotency_key or "").strip()
        if not self.idempotency_key:
            raise ValueError("Provider recovery requires an idempotency key.")
        self._state = self._load()

    @property
    def state(self) -> ProviderRecoveryState:
        return self._state.model_copy(deep=True)

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return [dict(receipt.payload) for receipt in self._state.receipts]

    def record_receipt(self, raw_receipt: Mapping[str, Any]) -> ProviderRecoveryState:
        """Persist one successful mutation receipt before downstream work continues."""

        receipt = normalize_provider_mutation_receipt(raw_receipt)
        status = str(receipt.payload.get("status") or "").strip().lower()
        if (
            status in _FAILED_MUTATION_STATUSES
            or not receipt_reports_possible_write(receipt.payload)
            or not receipt.object_id
            or receipt.verification_passed is not True
        ):
            raise ProviderRecoveryError(
                "Only a non-dry-run, identity-bearing, provider-verified mutation "
                "receipt may be checkpointed."
            )
        existing = next(
            (
                item
                for item in self._state.receipts
                if item.tool_name == receipt.tool_name
                and item.operation == receipt.operation
            ),
            None,
        )
        if existing is not None:
            if (
                existing.object_id
                and receipt.object_id
                and existing.object_id != receipt.object_id
            ):
                raise ProviderRecoveryConflictError(
                    "The idempotency key resolved to a different provider object."
                )
            return self.state
        completed_stages = [
            *self._state.completed_stages,
            f"provider_mutation:{receipt.operation}",
        ]
        self._replace_state(
            status="partial_success",
            receipts=[*self._state.receipts, receipt],
            completed_stages=list(dict.fromkeys(completed_stages)),
            failed_stage="",
            failure_code="",
            failure_summary="",
            safe_next_action=(
                "Resume from the saved provider receipt; do not recreate the object."
            ),
        )
        self._persist()
        return self.state

    def reuse_or_execute_mutation(
        self,
        *,
        tool_name: str,
        operation: str,
        execute: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Reuse a matching receipt or execute and checkpoint one mutation."""

        existing = next(
            (
                item
                for item in self._state.receipts
                if item.tool_name == tool_name and item.operation == operation
            ),
            None,
        )
        if existing is not None:
            self._replace_state(retry_reused=True)
            self._persist()
            return dict(existing.payload)
        raw_receipt = execute()
        receipt = {**dict(raw_receipt), "tool_name": tool_name}
        receipt.setdefault("operation", operation)
        self.record_receipt(receipt)
        return receipt

    def run_downstream_stage(self, stage: str, execute: Callable[[], T]) -> T:
        """Run a post-mutation stage and retain an actionable partial result on failure."""

        clean_stage = str(stage or "").strip()
        if not clean_stage:
            raise ValueError("Downstream recovery stages require a name.")
        try:
            result = execute()
        except Exception as exc:
            if not self._state.receipts:
                raise
            state = self.mark_failed(
                stage=clean_stage,
                failure_code=type(exc).__name__,
                failure_summary=str(exc),
            )
            raise ProviderPartialSuccessError(state, cause=exc) from exc
        self._replace_state(
            completed_stages=list(
                dict.fromkeys([*self._state.completed_stages, clean_stage])
            ),
            failed_stage="",
            failure_code="",
            failure_summary="",
        )
        self._persist()
        return result

    def mark_failed(
        self,
        *,
        stage: str,
        failure_code: str,
        failure_summary: str,
    ) -> ProviderRecoveryState:
        if not self._state.receipts:
            return self.state
        self._replace_state(
            status="partial_success",
            failed_stage=str(stage or "downstream_execution").strip(),
            failure_code=str(failure_code or "downstream_failure").strip(),
            failure_summary=str(failure_summary or "").strip()[:1000],
            safe_next_action=(
                "Retry with the same idempotency key so the saved provider object "
                "is reused and execution resumes at the failed stage."
            ),
        )
        self._persist()
        return self.state

    def mark_completed(self) -> ProviderRecoveryState:
        if not self._state.receipts:
            raise ProviderRecoveryError(
                "Provider recovery cannot complete without a mutation receipt."
            )
        self._replace_state(
            status="completed",
            failed_stage="",
            failure_code="",
            failure_summary="",
            safe_next_action="",
        )
        self._persist()
        return self.state

    def _load(self) -> ProviderRecoveryState:
        if not self.path.exists():
            return ProviderRecoveryState(idempotency_key=self.idempotency_key)
        try:
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderRecoveryError("Provider recovery checkpoint is unreadable.") from exc
        if not isinstance(wrapper, Mapping) or wrapper.get("schema_name") != _CHECKPOINT_SCHEMA:
            raise ProviderRecoveryError("Provider recovery checkpoint schema is invalid.")
        payload = wrapper.get("payload")
        if not isinstance(payload, Mapping):
            raise ProviderRecoveryError("Provider recovery checkpoint payload is missing.")
        expected_digest = str(wrapper.get("payload_sha256") or "")
        actual_digest = payload_digest(payload)
        if expected_digest != actual_digest:
            raise ProviderRecoveryError("Provider recovery checkpoint checksum mismatch.")
        state = ProviderRecoveryState.model_validate(payload)
        if state.idempotency_key != self.idempotency_key:
            raise ProviderRecoveryConflictError(
                "Provider recovery checkpoint belongs to another idempotency key."
            )
        return state

    def _persist(self) -> None:
        payload = self._state.model_dump(mode="json")
        wrapper = {
            "schema_name": _CHECKPOINT_SCHEMA,
            "payload": payload,
            "payload_sha256": payload_digest(payload),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(wrapper, handle, ensure_ascii=True, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _replace_state(self, **updates: Any) -> None:
        self._state = ProviderRecoveryState.model_validate(
            {**self._state.model_dump(mode="json"), **updates}
        )


def failure_stage_from_exception(exc: BaseException) -> str:
    """Classify the failed post-mutation stage without hiding provider success."""

    text = f"{type(exc).__name__} {exc}".lower()
    if "attachment" in text:
        return "attachment"
    if "read-back" in text or "read back" in text or "verification" in text:
        return "provider_verification"
    if "render" in text:
        return "rendering"
    if "structured" in text or "validation" in text:
        return "structured_output"
    return "sdk_execution"


def verified_provider_write_summary(
    receipts: Sequence[Mapping[str, Any]],
) -> str:
    """Summarize one current provider-verified mutation without model inference."""

    for raw_receipt in reversed(receipts):
        receipt = dict(raw_receipt)
        status = str(receipt.get("status") or "").strip().lower()
        verification = receipt.get("verification")
        if not (
            status in {"completed", "done", "ok", "success", "succeeded"}
            and isinstance(verification, Mapping)
            and verification.get("passed") is True
            and receipt_reports_possible_write(receipt)
        ):
            continue
        try:
            normalized = normalize_provider_mutation_receipt(receipt)
        except (TypeError, ValueError):
            continue
        if not normalized.tool_name or not normalized.operation or not normalized.object_id:
            continue
        operation = normalized.operation.replace("_", " ")
        table = str(receipt.get("table") or "").strip()
        target = " ".join(part for part in (table, normalized.object_id) if part).strip()
        duplicate_id = str(receipt.get("duplicate_record_id") or "").strip()
        if operation == "reconcile duplicate expense":
            summary = (
                "Reconciled and provider-verified the exact Airtable expense record "
                f"{normalized.object_id} in place"
            )
            if duplicate_id:
                summary += f"; duplicate {duplicate_id} was removed and verified absent"
            return summary + "."
        if operation in {"update", "update record", "update row"}:
            return f"Updated and provider-verified {target} in place."
        if operation in {
            "create",
            "create expense from receipt",
            "create sheet",
            "create note",
        }:
            return f"Created and provider-verified {target}."
        return (
            f"Completed and provider-verified the requested {operation} operation "
            f"for {target}."
        )
    return ""


__all__ = [
    "ProviderPartialSuccessError",
    "ProviderRecoveryConflictError",
    "ProviderRecoveryError",
    "ProviderRecoveryStore",
    "failure_stage_from_exception",
    "verified_provider_write_summary",
]
