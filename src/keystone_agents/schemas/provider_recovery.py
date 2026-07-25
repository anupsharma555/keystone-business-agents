"""Typed partial-success and idempotent-resume contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderMutationReceipt(BaseModel):
    """Bounded provider identity retained after one completed mutation."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    tool_name: str
    operation: str
    provider: str = ""
    object_id: str = ""
    provider_link: str = ""
    verification_passed: bool | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "receipt_id",
        "tool_name",
        "operation",
        "provider",
        "object_id",
        "provider_link",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _require_mutation_identity(self) -> ProviderMutationReceipt:
        if not self.receipt_id or not self.tool_name or not self.operation:
            raise ValueError("Mutation receipts require receipt, tool, and operation identity.")
        return self


class ProviderRecoveryState(BaseModel):
    """Durable state for a provider mutation whose full request may be incomplete."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.provider_recovery.v1"] = (
        "keystone.provider_recovery.v1"
    )
    idempotency_key: str
    status: Literal["ready", "partial_success", "completed"] = "ready"
    receipts: list[ProviderMutationReceipt] = Field(default_factory=list)
    completed_stages: list[str] = Field(default_factory=list)
    failed_stage: str = ""
    failure_code: str = ""
    failure_summary: str = ""
    safe_next_action: str = ""
    retry_reused: bool = False

    @field_validator(
        "idempotency_key",
        "failed_stage",
        "failure_code",
        "failure_summary",
        "safe_next_action",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("completed_stages", mode="before")
    @classmethod
    def _clean_stages(cls, value: object) -> list[str]:
        if not isinstance(value, list | tuple):
            return []
        return list(
            dict.fromkeys(
                str(item or "").strip()
                for item in value
                if str(item or "").strip()
            )
        )

    @model_validator(mode="after")
    def _validate_state(self) -> ProviderRecoveryState:
        if not self.idempotency_key:
            raise ValueError("Provider recovery requires an idempotency key.")
        if self.status == "partial_success" and not self.receipts:
            raise ValueError("Partial success requires at least one mutation receipt.")
        if self.status == "completed" and self.failed_stage:
            raise ValueError("Completed recovery state cannot retain a failed stage.")
        if self.failed_stage and self.status != "partial_success":
            raise ValueError("A failed stage requires partial-success state.")
        if self.failed_stage and not self.safe_next_action:
            raise ValueError("A failed stage requires a safe next action.")
        return self


__all__ = ["ProviderMutationReceipt", "ProviderRecoveryState"]
