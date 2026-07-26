"""Canonical authority for natural-language execution plans and stage outputs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from keystone_agents.schemas.manual_request_plan import (
    ManualProviderActionStep,
    ManualRequestPlan,
)

_INTENTS_BY_ROUTE: dict[str, frozenset[str]] = {
    "business_research_analyst": frozenset({"company_research", "research_brief"}),
    "opportunity_scout": frozenset(
        {"opportunity_search", "opportunity_to_outreach_loop"}
    ),
    "outreach_composer": frozenset(
        {"outreach_draft", "opportunity_to_outreach_loop"}
    ),
    "gmail_triage": frozenset({"gmail_triage"}),
}

_ARTIFACTS_BY_ROUTE: dict[str, frozenset[str]] = {
    "business_research_analyst": frozenset({"research_brief", "source_summary"}),
    "opportunity_scout": frozenset({"opportunity_record", "contact_candidates"}),
    "outreach_composer": frozenset({"outreach_draft"}),
    "gmail_triage": frozenset({"gmail_triage_report"}),
}

_CANONICAL_PLAN_SOURCES = frozenset(
    {
        "llm",
        "canonical",
        "orchestrator_canonical",
        "invalid_supplied_plan",
    }
)

_READ_ONLY_PROVIDER_OPERATIONS = frozenset({"read", "search", "verify"})

_PROVIDER_MUTATION_CLAIM_FIELDS = frozenset(
    {
        "provider_write",
        "provider_write_attempted",
        "write_executed",
        "draft_created",
        "gmail_draft_created",
        "send_enabled",
        "sent",
        "posted",
        "labels_modified",
        "record_created",
        "record_updated",
        "record_deleted",
        "event_created",
        "event_updated",
        "event_deleted",
        "document_created",
        "document_updated",
        "document_trashed",
    }
)


def _is_canonical_source(source: str) -> bool:
    clean_source = str(source or "").strip().lower()
    return clean_source in _CANONICAL_PLAN_SOURCES or clean_source.startswith(
        "canonical:"
    )


class StageOperationBoundary(StrEnum):
    """Maximum side-effect authority granted to one execution stage."""

    READ_ONLY = "read_only"
    DRAFT_ONLY = "draft_only"
    APPROVAL_CHECKPOINT = "approval_checkpoint"
    PROVIDER_WRITE = "provider_write"


@dataclass(frozen=True)
class StageOutputContract:
    """Typed authority for one model/helper phase, independent of task wording."""

    stage: str
    operation_boundary: StageOperationBoundary
    discard_fields: frozenset[str] = frozenset()
    public_output: bool = False


@dataclass(frozen=True)
class StageOutputReconciliation:
    """Sanitized phase payload plus exact discarded or prohibited field paths."""

    payload: dict[str, Any]
    discarded_paths: tuple[str, ...] = ()
    observed_effect_paths: tuple[str, ...] = ()
    prohibited_effect_paths: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.prohibited_effect_paths


def reconcile_stage_output(
    value: Mapping[str, Any] | Any,
    *,
    contract: StageOutputContract,
) -> StageOutputReconciliation:
    """Apply one phase boundary without treating harmless prose as a provider write.

    Draft text can be legal model output even when the current phase is only
    ranking or review. Callers name fields that are irrelevant to the phase in
    ``discard_fields``; those fields are cleared and audited. Exact structured
    provider-mutation claims remain hard violations for read, draft-only, and
    approval phases. Provider receipts, not prose, prove a real write.
    """

    payload_value: Any = value
    if not isinstance(payload_value, Mapping):
        model_dump = getattr(payload_value, "model_dump", None)
        if callable(model_dump):
            payload_value = model_dump(mode="python")
    if not isinstance(payload_value, Mapping):
        raise TypeError("Stage output reconciliation requires a mapping or model.")

    payload = deepcopy(dict(payload_value))
    discarded_paths: list[str] = []
    observed_effect_paths: list[str] = []
    prohibited_effect_paths: list[str] = []
    mutation_claims_allowed = (
        contract.operation_boundary is StageOperationBoundary.PROVIDER_WRITE
    )

    def walk(current: Any, *, path: str = "") -> None:
        if isinstance(current, dict):
            for key, item in list(current.items()):
                key_text = str(key)
                item_path = f"{path}.{key_text}" if path else key_text
                if key_text in contract.discard_fields and _has_stage_output_value(item):
                    current[key] = _discarded_stage_output_value(key_text, item)
                    discarded_paths.append(item_path)
                    continue
                if (
                    key_text in _PROVIDER_MUTATION_CLAIM_FIELDS
                    and bool(item)
                ):
                    observed_effect_paths.append(item_path)
                    if not mutation_claims_allowed:
                        prohibited_effect_paths.append(item_path)
                walk(item, path=item_path)
            return
        if isinstance(current, list):
            for index, item in enumerate(current):
                walk(item, path=f"{path}[{index}]")

    walk(payload)
    return StageOutputReconciliation(
        payload=payload,
        discarded_paths=tuple(discarded_paths),
        observed_effect_paths=tuple(observed_effect_paths),
        prohibited_effect_paths=tuple(prohibited_effect_paths),
    )


def _has_stage_output_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, tuple, set, dict)):
        return bool(value)
    return bool(value)


def _discarded_stage_output_value(field_name: str, value: Any) -> Any:
    if isinstance(value, bool):
        return False
    if field_name.endswith("_count") or isinstance(value, int):
        return 0
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return None


@dataclass(frozen=True)
class ExecutionIntentAuthority:
    """Classify supplied planning state before an executor interprets prose.

    There are four states:

    * missing: no plan exists, so a compatibility interpreter may run;
    * compatibility: a validated legacy/heuristic plan is a hint and may be
      supplemented by the older bounded parser;
    * canonical: Orchestrator meaning is authoritative for routes, providers,
      tool families, graph admission, and semantic completion requirements;
    * invalid canonical: fail closed without reopening keyword inference.

    Executors may still parse exact fields such as a date, title, sender, or
    record identifier after canonical provider admission. They must not use
    those words to select a different capability or manufacture a blocker for
    an unrequested one.
    """

    supplied: bool
    plan: ManualRequestPlan | None = None
    error: str = ""
    compatibility_only: bool = False
    supplied_fields: frozenset[str] = frozenset()

    @classmethod
    def from_value(cls, value: Any) -> ExecutionIntentAuthority:
        if value is None:
            return cls(supplied=False)
        if isinstance(value, ManualRequestPlan):
            return cls(
                supplied=True,
                plan=value,
                supplied_fields=frozenset(value.model_fields_set),
            )
        payload: Any = value
        if not isinstance(payload, Mapping):
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                payload = model_dump(mode="python")
        supplied_fields = frozenset(
            str(key) for key in payload
        ) if isinstance(payload, Mapping) else frozenset()
        source = (
            str(payload.get("source") or "").strip().lower()
            if isinstance(payload, Mapping)
            else ""
        )
        try:
            return cls(
                supplied=True,
                plan=ManualRequestPlan.model_validate(payload),
                supplied_fields=supplied_fields,
            )
        except (TypeError, ValueError) as exc:
            return cls(
                supplied=True,
                error=f"invalid_manual_request_plan:{type(exc).__name__}",
                compatibility_only=bool(
                    isinstance(payload, Mapping)
                    and not _is_canonical_source(source)
                ),
                supplied_fields=supplied_fields,
            )

    @property
    def canonical(self) -> bool:
        return bool(
            self.plan is not None
            and _is_canonical_source(self.plan.source)
        )

    @property
    def compatibility(self) -> bool:
        return self.compatibility_only or (
            self.plan is not None and not self.canonical
        )

    @property
    def invalid(self) -> bool:
        return self.supplied and self.plan is None and not self.compatibility_only

    @property
    def fallback_allowed(self) -> bool:
        return not self.supplied or self.compatibility

    def field_supplied(self, field_name: str) -> bool:
        """Return whether the producer explicitly supplied one plan field."""

        return str(field_name or "").strip() in self.supplied_fields

    def requests_route(self, route: str) -> bool:
        plan = self.plan
        clean_route = str(route or "").strip()
        if not self.canonical or plan is None or not clean_route:
            return False
        requested_routes = {plan.target_agent, *plan.workflow}
        if clean_route in requested_routes:
            return True
        if plan.intent in _INTENTS_BY_ROUTE.get(clean_route, frozenset()):
            return True
        return plan.expected_artifact_type in _ARTIFACTS_BY_ROUTE.get(
            clean_route, frozenset()
        )

    def authorizes_provider(
        self,
        provider: str,
        *,
        allowed_agents: set[str] | frozenset[str] | None = None,
        allowed_intents: set[str] | frozenset[str] | None = None,
    ) -> bool:
        plan = self.plan
        if (
            not self.canonical
            or plan is None
            or plan.provider_system != str(provider or "").strip()
        ):
            return False
        if allowed_agents is not None and plan.target_agent not in allowed_agents:
            return False
        if allowed_intents is not None and plan.intent not in allowed_intents:
            return False
        return True

    def effective_provider_operations(self, provider: str) -> tuple[str, ...]:
        """Return the typed provider operations after the permission ceiling.

        Provider operations describe the requested capability, while
        ``ask_shape.permission_state`` sets the maximum execution authority.
        A contradictory stored, replayed, or model-produced read-only plan
        therefore cannot admit a provider mutation even if its raw operation
        list still contains one.
        """

        plan = self.plan
        if (
            plan is None
            or plan.provider_system != str(provider or "").strip()
        ):
            return ()
        operations = tuple(dict.fromkeys(plan.provider_operations))
        if plan.ask_shape.permission_state == "read_only":
            return tuple(
                operation
                for operation in operations
                if operation in _READ_ONLY_PROVIDER_OPERATIONS
            )
        return operations

    def provider_action_steps(
        self,
        provider: str,
    ) -> tuple[ManualProviderActionStep, ...]:
        """Return canonical provider-object steps bounded by coarse operations.

        ``provider_operations`` remains the authorization ceiling. The action
        steps only refine which provider object and tool family can implement
        an already-selected operation; they cannot add a mutation or switch
        providers.
        """

        plan = self.plan
        if (
            not self.canonical
            or plan is None
            or plan.provider_system != str(provider or "").strip()
        ):
            return ()
        allowed_operations = set(self.effective_provider_operations(provider))
        return tuple(
            step
            for step in plan.provider_action_steps
            if step.operation in allowed_operations
        )

    def requests_contact_enrichment(self) -> bool:
        """Return whether canonical meaning asks for contact-candidate evidence.

        Words such as ``contact``, ``email``, and ``outreach`` may appear in
        quoted, historical, negated, or explanatory context. They must not
        enlarge a Business Research tool/artifact profile after planning. The
        typed task objective and expected artifact are the semantic authority;
        exact provider/source validation remains downstream.
        """

        plan = self.plan
        if not self.canonical or plan is None:
            return False
        return bool(
            plan.task_objective == "contact_discovery"
            or plan.expected_artifact_type == "contact_candidates"
        )

    def requests_internal_slack_artifact(self) -> bool:
        """Return whether canonical output is draft copy for an internal Slack audience."""

        plan = self.plan
        if not self.canonical or plan is None:
            return False
        channel = "_".join(
            str(plan.outreach_channel or "").strip().lower().replace("-", " ").split()
        )
        internal_audience = bool(
            plan.ask_shape.audience_scope == "internal"
            or channel
            in {
                "slack",
                "slack_only",
                "slack_thread",
                "team_channel",
                "internal_slack",
                "internal_team_slack",
                "internal_team_channel",
            }
        )
        outreach_owned = bool(
            plan.target_agent == "outreach_composer"
            or "outreach_composer" in plan.workflow
        )
        return bool(
            internal_audience
            and outreach_owned
            and not plan.recipient
            and (
                plan.task_objective == "outreach_draft"
                or plan.expected_artifact_type == "outreach_draft"
            )
            and plan.provider_system in {"unspecified", "slack"}
            and not plan.provider_operations
            and plan.side_effect_policy == "draft_or_read_only"
        )


def coerce_canonical_manual_request_plan(value: Any) -> ManualRequestPlan | None:
    """Return a validated supplied plan without enabling prose fallback."""

    authority = ExecutionIntentAuthority.from_value(value)
    return authority.plan if authority.canonical else None


__all__ = [
    "ExecutionIntentAuthority",
    "StageOperationBoundary",
    "StageOutputContract",
    "StageOutputReconciliation",
    "coerce_canonical_manual_request_plan",
    "reconcile_stage_output",
]
