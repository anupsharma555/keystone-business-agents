"""Request-scoped prompt, tool, model, and safety capability receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.contracts.completion import blocking_request_coverage
from keystone_agents.schemas.execution_request import ExecutionEntrypoint
from keystone_agents.schemas.request_coverage import RequestCoverage


class RequestCapabilityProfile(BaseModel):
    """Audit-safe effective profile for one bounded agent request."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.request_capability_profile.v1"
    entrypoint: ExecutionEntrypoint
    agent_name: str
    execution_shape: str
    prompt_profile: str
    prompt_chars: int = Field(ge=0)
    prompt_sha256: str
    model_provider: str = ""
    model_name: str
    max_turns: int = Field(ge=1)
    tool_names: tuple[str, ...] = ()
    tool_count: int = Field(ge=0)
    retrieval_enabled: bool = False
    provider_operations: tuple[str, ...] = ()
    write_enabled: bool = False
    send_enabled: bool = False
    profile_fingerprint: str

    @model_validator(mode="after")
    def _validate_effective_profile(self) -> RequestCapabilityProfile:
        if self.tool_count != len(self.tool_names):
            raise ValueError("tool_count must equal the exact effective tool-name count")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("effective tool names must be unique")
        write_operations = {"create", "update", "delete", "attach"}
        if write_operations.intersection(self.provider_operations) and not self.write_enabled:
            raise ValueError("provider mutations require write_enabled=true")
        if self.send_enabled and not self.write_enabled:
            raise ValueError("send_enabled=true requires write_enabled=true")
        return self

    def receipt(self) -> dict[str, Any]:
        """Return the stable JSON receipt stored with SDK request metadata."""

        return self.model_dump(mode="json")


class ChildResultPromotionReceipt(BaseModel):
    """Evidence that a successful child result is safe to promote for readers."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.child_result_promotion_receipt.v1"
    output_type: str = ""
    reader_ready: bool = False
    summary_sha256: str = ""
    verification_basis: tuple[str, ...] = ()
    child_public_result_verified: bool = False
    instruction_repair_verified: bool = False
    typed_display_verified: bool = False
    rendered_display_verified: bool = False
    send_enabled: bool = False
    provider_write_attempted: bool = False
    provider_receipt_verified: bool | None = None
    capability_profile_fingerprint: str = ""

    @model_validator(mode="after")
    def _validate_promotion(self) -> ChildResultPromotionReceipt:
        if self.reader_ready and not self.summary_sha256:
            raise ValueError("reader-ready promotion requires a summary fingerprint")
        if self.reader_ready and not self.verification_basis:
            raise ValueError("reader-ready promotion requires a verification basis")
        if self.reader_ready and self.send_enabled:
            raise ValueError("send-enabled child results cannot be promoted")
        if (
            self.reader_ready
            and self.provider_write_attempted
            and self.provider_receipt_verified is not True
        ):
            raise ValueError(
                "provider-write child promotion requires verified provider receipt"
            )
        return self

    def receipt(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def compile_request_capability_profile(
    *,
    entrypoint: ExecutionEntrypoint,
    agent: Any,
    execution_shape: str,
    prompt_profile: str,
    max_turns: int,
    retrieval_enabled: bool = False,
    provider_operations: Sequence[str] = (),
    write_enabled: bool = False,
    send_enabled: bool = False,
    model_provider: str = "",
    model_name: str | None = None,
) -> RequestCapabilityProfile:
    """Compile an exact, entrypoint-neutral effective profile from an SDK agent."""

    instructions = str(getattr(agent, "instructions", "") or "")
    tool_names = tuple(
        dict.fromkeys(
            tool_name_for_policy(tool).strip()
            for tool in list(getattr(agent, "tools", []) or [])
            if tool_name_for_policy(tool).strip()
        )
    )
    normalized_operations = tuple(
        dict.fromkeys(
            str(operation or "").strip().lower()
            for operation in provider_operations
            if str(operation or "").strip()
        )
    )
    fingerprint_payload = {
        # Entrypoint is intentionally excluded. Equivalent asks must compile to
        # the same effective profile regardless of their transport adapter.
        "agent_name": str(getattr(agent, "name", "") or "").strip(),
        "execution_shape": str(execution_shape or "").strip(),
        "prompt_profile": str(prompt_profile or "").strip(),
        "prompt_sha256": _sha256(instructions),
        "model_provider": str(model_provider or "").strip(),
        "model_name": str(
            model_name
            if model_name is not None
            else getattr(agent, "model", "") or ""
        ).strip(),
        "max_turns": int(max_turns),
        "tool_names": tool_names,
        "retrieval_enabled": bool(retrieval_enabled),
        "provider_operations": normalized_operations,
        "write_enabled": bool(write_enabled),
        "send_enabled": bool(send_enabled),
    }
    return RequestCapabilityProfile(
        entrypoint=entrypoint,
        agent_name=fingerprint_payload["agent_name"],
        execution_shape=fingerprint_payload["execution_shape"],
        prompt_profile=fingerprint_payload["prompt_profile"],
        prompt_chars=len(instructions),
        prompt_sha256=fingerprint_payload["prompt_sha256"],
        model_provider=fingerprint_payload["model_provider"],
        model_name=fingerprint_payload["model_name"],
        max_turns=fingerprint_payload["max_turns"],
        tool_names=tool_names,
        tool_count=len(tool_names),
        retrieval_enabled=fingerprint_payload["retrieval_enabled"],
        provider_operations=normalized_operations,
        write_enabled=fingerprint_payload["write_enabled"],
        send_enabled=fingerprint_payload["send_enabled"],
        profile_fingerprint=_sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )


def compile_child_result_promotion_receipt(
    child_payload: Mapping[str, Any],
    *,
    summary: str,
    instruction_repair_verified: bool = False,
    typed_display_verified: bool = False,
    host_request_coverage: RequestCoverage | None = None,
) -> ChildResultPromotionReceipt:
    """Compile bounded evidence for promoting one successful child summary.

    The receipt does not review the specialist artifact or grant provider
    authority. It only records why a no-send child result may become the parent
    reader-facing result after typed rendering or constraint validation.
    """

    clean_summary = str(summary or "").strip()
    status = str(child_payload.get("status") or "completed").strip().lower()
    send_enabled = bool(child_payload.get("send_enabled"))
    public_result = child_payload.get("public_result")
    public_mapping = public_result if isinstance(public_result, Mapping) else {}
    child_public_result_verified = bool(
        child_payload.get("user_facing_result_verified") is True
        and public_mapping.get("completion_confirmed") is True
        and str(public_mapping.get("status") or "") == "completed"
    )
    provider_write_attempted = bool(
        public_mapping.get("provider_write_attempted") is True
    )
    provider_receipt_verified = (
        bool(public_mapping.get("provider_receipt_verified") is True)
        if provider_write_attempted
        else None
    )
    request_cache = child_payload.get("request_cache")
    request_cache_mapping = (
        request_cache if isinstance(request_cache, Mapping) else {}
    )
    capability_profile = request_cache_mapping.get("capability_profile")
    capability_mapping = (
        capability_profile if isinstance(capability_profile, Mapping) else {}
    )
    rendered_display_verified = bool(
        clean_summary
        and all(
            str(child_payload.get(field) or "").strip() == clean_summary
            for field in ("human_summary", "slack_display_text", "display_text", "summary")
        )
    )
    basis: list[str] = []
    if child_public_result_verified:
        basis.append("verified_child_public_result")
    if instruction_repair_verified:
        basis.append("validated_instruction_repair")
    if typed_display_verified:
        basis.append("typed_display_contract")
    if rendered_display_verified:
        basis.append("mirrored_child_display_contract")
    host_completion_blocked = bool(
        host_request_coverage is not None
        and blocking_request_coverage([host_request_coverage])
    )
    reader_ready = bool(
        clean_summary
        and not send_enabled
        and status in {"verified", "completed", "complete", "done", "success", "recovered"}
        and not host_completion_blocked
        and (not provider_write_attempted or provider_receipt_verified is True)
        and basis
    )
    return ChildResultPromotionReceipt(
        output_type=str(child_payload.get("output_type") or "").strip(),
        reader_ready=reader_ready,
        summary_sha256=_sha256(clean_summary) if clean_summary else "",
        verification_basis=tuple(basis),
        child_public_result_verified=child_public_result_verified,
        instruction_repair_verified=instruction_repair_verified,
        typed_display_verified=typed_display_verified,
        rendered_display_verified=rendered_display_verified,
        send_enabled=send_enabled,
        provider_write_attempted=provider_write_attempted,
        provider_receipt_verified=provider_receipt_verified,
        capability_profile_fingerprint=str(
            capability_mapping.get("profile_fingerprint") or ""
        ).strip(),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ChildResultPromotionReceipt",
    "RequestCapabilityProfile",
    "compile_child_result_promotion_receipt",
    "compile_request_capability_profile",
]
