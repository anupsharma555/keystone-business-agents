"""Request-scoped prompt, tool, model, and safety capability receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keystone_agents.capabilities.catalog import instruction_profile_text
from keystone_agents.schemas.execution_request import ExecutionEntrypoint


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


class RuntimeOwnerScope(BaseModel):
    """One owner and its safe, effective read/tool scope for route selection."""

    model_config = ConfigDict(frozen=True)

    route: str
    tool_names: tuple[str, ...] = ()
    source_kinds: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_owner_scope(self) -> RuntimeOwnerScope:
        if not self.route.strip():
            raise ValueError("runtime owner scope requires a route")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("runtime owner tool names must be unique")
        if len(set(self.source_kinds)) != len(self.source_kinds):
            raise ValueError("runtime owner source kinds must be unique")
        return self


class EffectiveRuntimeRouteScope(BaseModel):
    """Safe route-time view of an active restrictive runtime profile.

    This deliberately excludes paths, digests, credentials, source contents,
    budgets, and any preferred or gold route.  It tells the Orchestrator what
    is currently available; the Orchestrator still owns the semantic choice.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.effective_runtime_route_scope.v1"
    restriction_active: bool = True
    scope_source: str
    owner_scopes: tuple[RuntimeOwnerScope, ...] = ()

    @model_validator(mode="after")
    def _validate_runtime_scope(self) -> EffectiveRuntimeRouteScope:
        routes = tuple(owner.route for owner in self.owner_scopes)
        if len(set(routes)) != len(routes):
            raise ValueError("runtime owner routes must be unique")
        if not self.scope_source.strip():
            raise ValueError("runtime scope requires a safe source label")
        return self

    @property
    def permitted_routes(self) -> tuple[str, ...]:
        return tuple(owner.route for owner in self.owner_scopes)

    def prompt_payload(
        self,
        *,
        route_candidates: Iterable[str],
        unavailable_requested_owner: str = "",
    ) -> dict[str, Any]:
        """Return only scope details relevant to the current candidate universe."""

        candidates = tuple(dict.fromkeys(str(route).strip() for route in route_candidates))
        candidate_set = set(candidates)
        owners = [
            owner.model_dump(mode="json")
            for owner in self.owner_scopes
            if owner.route in candidate_set
        ]
        return {
            "schema_name": self.schema_name,
            "restriction_active": self.restriction_active,
            "scope_source": self.scope_source,
            "permitted_routes": [
                route for route in candidates if route in set(self.permitted_routes)
            ],
            "owner_scopes": owners,
            "unavailable_requested_owner": unavailable_requested_owner,
        }


def compile_runtime_route_scope(
    *,
    scope_source: str,
    allowed_agents: Iterable[object],
    allowed_function_tools: Mapping[str, Iterable[object]] | None = None,
    source_kinds_by_agent: Mapping[str, Iterable[object]] | None = None,
) -> EffectiveRuntimeRouteScope:
    """Compile a generic, model-visible scope from an authoritative runtime ceiling."""

    tool_map = allowed_function_tools or {}
    source_map = source_kinds_by_agent or {}
    routes = tuple(
        dict.fromkeys(
            str(agent or "").strip()
            for agent in allowed_agents
            if str(agent or "").strip()
        )
    )
    owners = tuple(
        RuntimeOwnerScope(
            route=route,
            tool_names=tuple(
                dict.fromkeys(
                    str(name or "").strip()
                    for name in tool_map.get(route, ())
                    if str(name or "").strip()
                )
            ),
            source_kinds=tuple(
                dict.fromkeys(
                    str(kind or "").strip()
                    for kind in source_map.get(route, ())
                    if str(kind or "").strip()
                )
            ),
        )
        for route in routes
    )
    return EffectiveRuntimeRouteScope(
        scope_source=str(scope_source or "").strip(),
        owner_scopes=owners,
    )


def active_runtime_route_scope() -> EffectiveRuntimeRouteScope | None:
    """Return the active canary ceiling as safe route-time metadata, if present."""

    # Import lazily so ordinary agent construction does not load acceptance
    # runtime machinery or create a capability-profile import cycle.
    from keystone_agents.canary_acceptance import load_profile

    profile = load_profile()
    if profile is None or not profile.enabled:
        return None
    source_kinds_by_agent: dict[str, tuple[str, ...]] = {}
    if profile.is_public_preprint_scenario:
        source_kinds_by_agent["preprints_context_agent"] = (
            "local_saved_preprint_history",
        )
    else:
        source_kinds_by_agent["gmail_triage"] = (
            "authenticated_read_only_mailbox",
        )
    return compile_runtime_route_scope(
        scope_source="reviewed_acceptance_profile",
        allowed_agents=sorted(profile.allowed_agents),
        allowed_function_tools={
            agent: sorted(profile.allowed_function_tools(agent))
            for agent in profile.allowed_agents
        },
        source_kinds_by_agent=source_kinds_by_agent,
    )


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
) -> RequestCapabilityProfile:
    """Compile an exact, entrypoint-neutral effective profile from an SDK agent."""

    instructions = instruction_profile_text(agent)
    if instructions is None:
        raise ValueError(
            "A dynamic instruction callable needs an explicit static instruction base."
        )
    tool_names = tuple(
        dict.fromkeys(
            str(getattr(tool, "name", "") or getattr(tool, "__name__", "")).strip()
            for tool in list(getattr(agent, "tools", []) or [])
            if str(
                getattr(tool, "name", "") or getattr(tool, "__name__", "")
            ).strip()
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
        "model_name": str(getattr(agent, "model", "") or "").strip(),
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
) -> ChildResultPromotionReceipt:
    """Compile bounded evidence for promoting one successful child summary.

    The receipt does not review the specialist artifact or grant provider
    authority. It only records why a no-send child result may become the parent
    reader-facing result after typed rendering or constraint validation.
    """

    clean_summary = str(summary or "").strip()
    status = str(child_payload.get("status") or "completed").strip().lower()
    send_enabled = bool(child_payload.get("send_enabled"))
    explicitly_unverified = child_result_explicitly_unverified(child_payload)
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
    if instruction_repair_verified and not explicitly_unverified:
        basis.append("validated_instruction_repair")
    if typed_display_verified and not explicitly_unverified:
        basis.append("typed_display_contract")
    if rendered_display_verified and not explicitly_unverified:
        basis.append("mirrored_child_display_contract")
    reader_ready = bool(
        clean_summary
        and not explicitly_unverified
        and not send_enabled
        and status not in {"blocked", "clarification_required", "failed", "needs_input"}
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


def child_result_explicitly_unverified(child_payload: Mapping[str, Any]) -> bool:
    """Keep an explicit child verification failure authoritative across adapters."""

    return child_payload.get("user_facing_result_verified") is False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ChildResultPromotionReceipt",
    "EffectiveRuntimeRouteScope",
    "RequestCapabilityProfile",
    "RuntimeOwnerScope",
    "active_runtime_route_scope",
    "compile_child_result_promotion_receipt",
    "child_result_explicitly_unverified",
    "compile_request_capability_profile",
    "compile_runtime_route_scope",
]
